from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass
from math import isfinite

from .approach_hil import ApproachHilPhase
from .compat import StrEnum
from .domain import (
    BoundingBox,
    DeploymentWindowStatus,
    MissionPhase,
    ReleaseTimingStatus,
    RuleCheck,
    Verdict,
)
from .multimodal_ranging import RangeSourceContribution, RangeValidity
from .patrol_advisory import AdvisoryValidity, PatrolPhase, ReturnObserveDirection
from .payload_target_gate import PayloadTargetEligibility
from .unified_tracking import UnifiedTrackState

OPERATOR_LINK_PROTOCOL_VERSION = 1
MAX_SELECTION_TTL_S = 5.0
MAX_AUTHORIZATION_DECISION_TTL_S = 5.0
SAFETY_RULE_REGISTRY_VERSION = 1
OPERATOR_SAFETY_RULE_IDS = (
    "target.confirmed_track",
    "target.allowed_class",
    "target.minimum_confidence",
    "sensor.frame_freshness",
    "sensor.track_freshness",
    "navigation.allowed_zone",
    "navigation.geofence_health",
    "navigation.position_health",
    "communications.link_health",
    "flight.allowed_mode",
    "deployment.release_zone_clear",
    "flight.altitude",
    "flight.roll",
    "flight.pitch",
    "flight.ground_speed",
    "deployment.fixed_wing_release_window",
    "sensor.person_detector_health",
    "deployment.person_exclusion",
    "sensor.independent_rgb_fire_consistency",
    "sensor.thermal_consistency",
)

RANGING_SOURCE_IDS = (
    "pixhawk_agl",
    "dem_gps",
    "ground_plane",
    "camera_ground",
    "laser",
    "vio",
    "monocular_size",
    "monocular_metric",
    "rgb_slam",
)

RANGING_REASON_IDS = (
    "pose_stale_or_from_future",
    "target_image_stale_or_from_future",
    "pose_image_time_skew_exceeded",
    "duplicate_vertical_source",
    "vertical_reference_unavailable_or_stale",
    "vertical_references_inconsistent",
    "vertical_reference_outlier_rejected",
    "target_ray_does_not_intersect_ground_safely",
    "camera_ground_intersection_out_of_range",
    "duplicate_direct_range_source",
    "laser_target_mismatch",
    "vio_target_mismatch",
    "laser_absolute_scale_invalid",
    "vio_absolute_scale_invalid",
    "laser_stale_or_from_future",
    "vio_stale_or_from_future",
    "laser_out_of_range",
    "vio_out_of_range",
    "absolute_range_sources_inconsistent",
    "absolute_range_outlier_rejected",
    "single_absolute_range_method",
    "multimodal_range_consistent",
    "primary_target_snapshot_unavailable",
    "primary_target_not_freshly_observed",
    "pixhawk_pose_or_timestamp_unavailable",
    "pixhawk_agl_unavailable",
    "attitude_position_time_skew_exceeded",
    "target_not_freshly_observed",
    "direct_degraded_metric_range",
    "vertical_reference_unavailable",
    "direct_range_unavailable",
)

RELEASE_REASON_IDS = (
    "target_class_not_eligible",
    "multimodal_range_evidence_unavailable",
    "range_target_class_mismatch",
    "range_target_spatial_binding_failed",
    "multimodal_range_evidence_stale",
    "multimodal_range_not_valid",
    "multimodal_range_consistency_too_low",
    "multimodal_range_freshness_invalid",
    "multimodal_range_geometry_incomplete",
    "ballistic_telemetry_unavailable",
    "ballistic_telemetry_out_of_domain",
    "ballistic_telemetry_stale_or_from_future",
    "airspeed_groundspeed_wind_inconsistent",
    "ballistic_integration_failed",
    "impact_uncertainty_exceeds_limit",
    "target_outside_cross_track_corridor",
    "before_release_window",
    "release_window_passed",
    "multimodal_release_window_ready",
    "required_telemetry_unavailable",
    "required_telemetry_out_of_domain",
    "target_outside_calibrated_ground_projection",
    "release_window_ready",
)

APPROACH_REASON_IDS = (
    "no_target_selected",
    "abort_latched_until_reselection",
    "target_binding_changed",
    "target_occluded",
    "target_reacquiring",
    "target_recovered",
    "target_lost",
    "target_not_stably_tracking",
    "target_evidence_stale",
    "slide_confirmation_required",
    "slide_confirmation_expired",
    "avoidance_unavailable",
    "avoidance_stale",
    "avoidance_avoid",
    "avoidance_invalid",
    "range_unavailable",
    "range_target_or_frame_mismatch",
    "range_invalid",
    "range_freshness_or_consistency_invalid",
    "range_outside_approach_domain",
    "navigation_or_link_unhealthy",
    "required_telemetry_unavailable",
    "required_telemetry_stale_or_from_future",
    "altitude_outside_approach_domain",
    "airspeed_below_approach_minimum",
    "roll_outside_approach_domain",
    "pitch_outside_approach_domain",
    "target_outside_approach_corridor",
    "approach_completion_gate_reached",
    "approach_corridor_centered",
    "centering_advice_only",
    "fixed_wing_aim_active",
)

if len(OPERATOR_SAFETY_RULE_IDS) > 32:
    raise RuntimeError("operator safety rule registry exceeds the 32-bit wire mask")
if len(set(OPERATOR_SAFETY_RULE_IDS)) != len(OPERATOR_SAFETY_RULE_IDS):
    raise RuntimeError("operator safety rule registry contains duplicate rule IDs")
if len(RANGING_REASON_IDS) > 32 or len(RANGING_SOURCE_IDS) > 16:
    raise RuntimeError("operator ranging registry exceeds its wire mask")
if len(set(RANGING_REASON_IDS)) != len(RANGING_REASON_IDS) or len(set(RANGING_SOURCE_IDS)) != len(
    RANGING_SOURCE_IDS
):
    raise RuntimeError("operator ranging registry contains duplicate IDs")
if len(RELEASE_REASON_IDS) > 32 or len(set(RELEASE_REASON_IDS)) != len(RELEASE_REASON_IDS):
    raise RuntimeError("operator release registry exceeds its wire mask or contains duplicates")
if len(APPROACH_REASON_IDS) > 32 or len(set(APPROACH_REASON_IDS)) != len(APPROACH_REASON_IDS):
    raise RuntimeError("operator approach registry exceeds its wire mask or contains duplicates")


class SelectionAction(StrEnum):
    SELECT = "select"
    SWITCH = "switch"
    CANCEL = "cancel"
    SELECT_TRK = "select_trk"
    PROMOTE_LCK = "promote_lck"
    DEMOTE_TRK = "demote_trk"
    CANCEL_TRK = "cancel_trk"


class TrackingState(StrEnum):
    INITIALIZING = "initializing"
    TRACKING = "tracking"
    LOST = "lost"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class AuthorizationDisplayState(StrEnum):
    """Read-only authorization state rendered by the ground application."""

    NONE = "none"
    PENDING = "pending"
    APPROVED = "approved"


class AuthorizationDecision(StrEnum):
    APPROVE = "approve"
    DENY = "deny"


def operator_identifier_token(value: str) -> int:
    """Return the stable 64-bit wire token used for bound operator metadata."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("operator identifier cannot be empty")
    return int.from_bytes(hashlib.sha256(value.strip().encode("utf-8")).digest()[:8], "big")


@dataclass(frozen=True, slots=True)
class VideoGeometry:
    """Identity and dimensions of the video surface used for normalized coordinates."""

    stream_id: str
    width: int
    height: int
    rotation_degrees: int = 0

    def __post_init__(self) -> None:
        if not self.stream_id.strip():
            raise ValueError("stream_id cannot be empty")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("video dimensions must be positive")
        if self.rotation_degrees not in {0, 90, 180, 270}:
            raise ValueError("rotation_degrees must be 0, 90, 180 or 270")


@dataclass(frozen=True, slots=True)
class TargetSelectionCommand:
    """A bounded operator selection; it is not an authorization to deploy a payload."""

    command_id: str
    session_id: str
    sequence: int
    action: SelectionAction
    geometry: VideoGeometry
    issued_at_s: float
    expires_at_s: float
    bbox: BoundingBox | None = None
    displayed_frame_id: str | None = None
    protocol_version: int = OPERATOR_LINK_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != OPERATOR_LINK_PROTOCOL_VERSION:
            raise ValueError("unsupported operator-link protocol version")
        if not self.command_id.strip():
            raise ValueError("command_id cannot be empty")
        if not self.session_id.strip():
            raise ValueError("session_id cannot be empty")
        if not 0 <= self.sequence <= 0xFFFFFFFF:
            raise ValueError("sequence must fit in an unsigned 32-bit integer")
        if not isfinite(self.issued_at_s) or not isfinite(self.expires_at_s):
            raise ValueError("command timestamps must be finite")
        ttl_s = self.expires_at_s - self.issued_at_s
        if ttl_s <= 0.0 or ttl_s > MAX_SELECTION_TTL_S:
            raise ValueError(f"selection TTL must be in (0, {MAX_SELECTION_TTL_S}] seconds")
        if self.action is SelectionAction.CANCEL and self.bbox is not None:
            raise ValueError("cancel commands cannot contain a bounding box")
        if self.action is not SelectionAction.CANCEL and self.bbox is None:
            raise ValueError("select and switch commands require a bounding box")


@dataclass(frozen=True, slots=True)
class SelectionAcceptance:
    allowed: bool
    reasons: tuple[str, ...]


class SelectionCommandGuard:
    """Reject stale, replayed or geometrically incompatible operator commands."""

    def __init__(
        self,
        active_geometry: VideoGeometry,
        *,
        clock_tolerance_s: float = 0.5,
        replay_window_size: int = 256,
    ) -> None:
        if not isfinite(clock_tolerance_s) or clock_tolerance_s < 0.0:
            raise ValueError("clock_tolerance_s must be finite and non-negative")
        if replay_window_size <= 0:
            raise ValueError("replay_window_size must be positive")
        self.active_geometry = active_geometry
        self.clock_tolerance_s = clock_tolerance_s
        self._seen_ids: set[str] = set()
        self._seen_order: deque[str] = deque(maxlen=replay_window_size)
        self._last_sequence_by_session: dict[str, int] = {}

    def evaluate(
        self, command: TargetSelectionCommand, *, received_at_s: float
    ) -> SelectionAcceptance:
        if not isfinite(received_at_s) or received_at_s < 0.0:
            raise ValueError("received_at_s must be finite and non-negative")

        reasons: list[str] = []
        geometry = command.geometry
        if geometry.stream_id != self.active_geometry.stream_id:
            reasons.append("selection stream does not match the active Jetson stream")
        if (geometry.width, geometry.height) != (
            self.active_geometry.width,
            self.active_geometry.height,
        ):
            reasons.append("selection source dimensions do not match the active stream")
        if geometry.rotation_degrees != self.active_geometry.rotation_degrees:
            reasons.append("selection rotation does not match the active stream")
        if received_at_s > command.expires_at_s + self.clock_tolerance_s:
            reasons.append("selection command is stale")
        if received_at_s < command.issued_at_s - self.clock_tolerance_s:
            reasons.append("selection command is dated in the future")
        if command.command_id in self._seen_ids:
            reasons.append("selection command ID has already been processed")
        previous_sequence = self._last_sequence_by_session.get(command.session_id)
        if previous_sequence is not None and command.sequence <= previous_sequence:
            reasons.append("selection sequence is not newer than the last accepted command")

        if reasons:
            return SelectionAcceptance(False, tuple(reasons))

        if len(self._seen_order) == self._seen_order.maxlen:
            oldest = self._seen_order[0]
            self._seen_ids.remove(oldest)
        self._seen_order.append(command.command_id)
        self._seen_ids.add(command.command_id)
        self._last_sequence_by_session[command.session_id] = command.sequence
        return SelectionAcceptance(True, ())


@dataclass(frozen=True, slots=True)
class AuthorizationChallengeStatusMessage:
    """Bound pending challenge metadata for G20; nonce and raw identifiers stay on Jetson."""

    challenge_token: int
    mission_token: int
    target_token: int
    scene_token: int
    ruleset_token: int
    payload_slot_token: int
    target_revision: int
    created_at_s: float
    expires_at_s: float
    sequence: int
    produced_at_s: float
    pending: bool = True
    protocol_version: int = OPERATOR_LINK_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != OPERATOR_LINK_PROTOCOL_VERSION:
            raise ValueError("unsupported operator-link protocol version")
        for name in (
            "challenge_token",
            "mission_token",
            "target_token",
            "scene_token",
            "ruleset_token",
            "payload_slot_token",
        ):
            _require_uint(getattr(self, name), bits=64, name=name, nonzero=True)
        _require_uint(self.target_revision, bits=32, name="target_revision")
        _require_uint(self.sequence, bits=32, name="sequence")
        timestamps = (self.created_at_s, self.expires_at_s, self.produced_at_s)
        if not all(isfinite(value) and value >= 0.0 for value in timestamps):
            raise ValueError("authorization challenge timestamps must be finite and non-negative")
        if self.expires_at_s <= self.created_at_s:
            raise ValueError("authorization challenge must expire after creation")
        if not isinstance(self.pending, bool):
            raise ValueError("authorization challenge pending flag must be boolean")


@dataclass(frozen=True, slots=True)
class AuthorizationDecisionCommand:
    """G20 decision bound to one exact challenge snapshot; never a payload command."""

    command_token: int
    session_token: int
    challenge_token: int
    mission_token: int
    target_token: int
    scene_token: int
    ruleset_token: int
    payload_slot_token: int
    target_revision: int
    decision: AuthorizationDecision
    operator_token: int
    sequence: int
    issued_at_s: float
    expires_at_s: float
    protocol_version: int = OPERATOR_LINK_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != OPERATOR_LINK_PROTOCOL_VERSION:
            raise ValueError("unsupported operator-link protocol version")
        for name in (
            "command_token",
            "session_token",
            "challenge_token",
            "mission_token",
            "target_token",
            "scene_token",
            "ruleset_token",
            "payload_slot_token",
            "operator_token",
        ):
            _require_uint(getattr(self, name), bits=64, name=name, nonzero=True)
        _require_uint(self.target_revision, bits=32, name="target_revision")
        _require_uint(self.sequence, bits=32, name="sequence")
        if not isinstance(self.decision, AuthorizationDecision):
            raise ValueError("authorization decision is invalid")
        if not isfinite(self.issued_at_s) or not isfinite(self.expires_at_s):
            raise ValueError("authorization decision timestamps must be finite")
        ttl_s = self.expires_at_s - self.issued_at_s
        if ttl_s <= 0.0 or ttl_s > MAX_AUTHORIZATION_DECISION_TTL_S:
            raise ValueError(
                "authorization decision TTL must be in "
                f"(0, {MAX_AUTHORIZATION_DECISION_TTL_S}] seconds"
            )


@dataclass(frozen=True, slots=True)
class AuthorizationDecisionAcceptance:
    allowed: bool
    reasons: tuple[str, ...]
    duplicate: bool = False


class AuthorizationDecisionCommandGuard:
    """Accept only one fresh, fully bound decision for the current pending challenge."""

    def __init__(
        self,
        *,
        clock_tolerance_s: float = 0.5,
        replay_window_size: int = 256,
    ) -> None:
        if not isfinite(clock_tolerance_s) or clock_tolerance_s < 0:
            raise ValueError("authorization clock tolerance must be finite and non-negative")
        if replay_window_size <= 0:
            raise ValueError("authorization replay window must be positive")
        self.clock_tolerance_s = clock_tolerance_s
        self._active: AuthorizationChallengeStatusMessage | None = None
        self._active_snapshots: deque[AuthorizationChallengeStatusMessage] = deque(maxlen=32)
        self._last_sequence_by_session: dict[int, int] = {}
        self._processed: dict[
            int,
            tuple[AuthorizationDecisionCommand, AuthorizationDecisionAcceptance],
        ] = {}
        self._processed_order: deque[int] = deque(maxlen=replay_window_size)
        self._decided_challenges: set[int] = set()

    def set_active_challenge(
        self,
        challenge: AuthorizationChallengeStatusMessage | None,
    ) -> None:
        if challenge is not None and not challenge.pending:
            raise ValueError("active authorization challenge must be pending")
        if challenge is None:
            self._active_snapshots.clear()
        elif self._active is None or self._active.challenge_token != challenge.challenge_token:
            self._active_snapshots.clear()
            self._active_snapshots.append(challenge)
        elif challenge not in self._active_snapshots:
            self._active_snapshots.append(challenge)
        self._active = challenge

    def evaluate(
        self,
        command: AuthorizationDecisionCommand,
        *,
        received_at_s: float,
    ) -> AuthorizationDecisionAcceptance:
        if not isfinite(received_at_s) or received_at_s < 0:
            raise ValueError("authorization receipt timestamp must be finite and non-negative")
        cached = self._processed.get(command.command_token)
        if cached is not None:
            previous, acceptance = cached
            if previous == command:
                return AuthorizationDecisionAcceptance(
                    acceptance.allowed,
                    acceptance.reasons,
                    duplicate=True,
                )
            return AuthorizationDecisionAcceptance(
                False,
                ("authorization command token was reused with different content",),
            )

        reasons: list[str] = []
        active = self._active
        if active is None:
            reasons.append("no pending authorization challenge is active")
        else:
            supplied = (
                command.challenge_token,
                command.mission_token,
                command.target_token,
                command.scene_token,
                command.ruleset_token,
                command.payload_slot_token,
                command.target_revision,
            )
            matching_snapshot = next(
                (
                    snapshot
                    for snapshot in self._active_snapshots
                    if supplied
                    == (
                        snapshot.challenge_token,
                        snapshot.mission_token,
                        snapshot.target_token,
                        snapshot.scene_token,
                        snapshot.ruleset_token,
                        snapshot.payload_slot_token,
                        snapshot.target_revision,
                    )
                ),
                None,
            )
            if matching_snapshot is None:
                reasons.append("authorization command does not match the active challenge")
            if received_at_s >= active.expires_at_s:
                reasons.append("authorization challenge has expired")
            binding_expiry_s = (
                active.expires_at_s
                if matching_snapshot is None
                else min(active.expires_at_s, matching_snapshot.expires_at_s)
            )
            if command.expires_at_s > binding_expiry_s + self.clock_tolerance_s:
                reasons.append("authorization command outlives its challenge")
            if active.challenge_token in self._decided_challenges:
                reasons.append("authorization challenge already has a decision")
        if received_at_s > command.expires_at_s + self.clock_tolerance_s:
            reasons.append("authorization command is stale")
        if received_at_s < command.issued_at_s - self.clock_tolerance_s:
            reasons.append("authorization command is dated in the future")
        previous_sequence = self._last_sequence_by_session.get(command.session_token)
        if previous_sequence is not None and command.sequence <= previous_sequence:
            reasons.append("authorization sequence is not newer than the last command")

        acceptance = AuthorizationDecisionAcceptance(not reasons, tuple(reasons))
        self._remember(command, acceptance)
        if acceptance.allowed:
            self._last_sequence_by_session[command.session_token] = command.sequence
            self._decided_challenges.add(command.challenge_token)
        return acceptance

    def _remember(
        self,
        command: AuthorizationDecisionCommand,
        acceptance: AuthorizationDecisionAcceptance,
    ) -> None:
        if len(self._processed_order) == self._processed_order.maxlen:
            oldest = self._processed_order[0]
            self._processed.pop(oldest, None)
        self._processed_order.append(command.command_token)
        self._processed[command.command_token] = (command, acceptance)


def _require_uint(value: object, *, bits: int, name: str, nonzero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 1 << bits:
        raise ValueError(f"{name} must fit in an unsigned {bits}-bit integer")
    if nonzero and value == 0:
        raise ValueError(f"{name} cannot be zero")
    return value


@dataclass(frozen=True, slots=True)
class TrackStatusMessage:
    """Tracking metadata for local overlay on G20; no video pixels are included."""

    status_id: str
    selection_command_id: str
    sequence: int
    geometry: VideoGeometry
    state: TrackingState
    target_id: str | None
    bbox: BoundingBox | None
    label: str | None
    confidence: float | None
    tracking_quality: float | None
    source_frame_id: str
    source_captured_at_s: float
    produced_at_s: float
    relative_bearing_deg: float | None = None
    estimated_range_m: float | None = None
    protocol_version: int = OPERATOR_LINK_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != OPERATOR_LINK_PROTOCOL_VERSION:
            raise ValueError("unsupported operator-link protocol version")
        if not self.status_id.strip() or not self.selection_command_id.strip():
            raise ValueError("status and selection command IDs cannot be empty")
        if not 0 <= self.sequence <= 0xFFFFFFFF:
            raise ValueError("sequence must fit in an unsigned 32-bit integer")
        if not self.source_frame_id.strip():
            raise ValueError("source_frame_id cannot be empty")
        timestamps = (self.source_captured_at_s, self.produced_at_s)
        if not all(isfinite(value) and value >= 0.0 for value in timestamps):
            raise ValueError("tracking timestamps must be finite and non-negative")
        if self.produced_at_s < self.source_captured_at_s:
            raise ValueError("tracking status cannot predate its source frame")
        if self.state in {TrackingState.INITIALIZING, TrackingState.TRACKING}:
            if self.target_id is None or self.bbox is None:
                raise ValueError("active tracking states require a target ID and bounding box")
        for name, value in (
            ("confidence", self.confidence),
            ("tracking_quality", self.tracking_quality),
        ):
            if value is not None and (not isfinite(value) or not 0.0 <= value <= 1.0):
                raise ValueError(f"{name} must be in [0, 1]")
        if self.relative_bearing_deg is not None and (
            not isfinite(self.relative_bearing_deg)
            or not -180.0 <= self.relative_bearing_deg <= 180.0
        ):
            raise ValueError("relative_bearing_deg must be in [-180, 180]")
        if self.estimated_range_m is not None and (
            not isfinite(self.estimated_range_m) or self.estimated_range_m < 0.0
        ):
            raise ValueError("estimated_range_m must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class MissionStatusMessage:
    """Compact mission metadata for G20 display; never a control command."""

    status_id: str
    sequence: int
    mission_id: str
    phase: MissionPhase
    authorization_state: AuthorizationDisplayState
    release_window: DeploymentWindowStatus | None
    safety_allowed: bool | None
    remaining_payload_count: int
    total_payload_count: int
    target_id: str | None
    active_payload_slot_id: str | None
    target_confidence: float | None
    relative_bearing_deg: float | None
    estimated_range_m: float | None
    cross_track_error_m: float | None
    along_track_error_m: float | None
    release_lead_distance_m: float | None
    produced_at_s: float
    advisory_only: bool = True
    flight_control_enabled: bool = False
    physical_release_enabled: bool = False
    protocol_version: int = OPERATOR_LINK_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != OPERATOR_LINK_PROTOCOL_VERSION:
            raise ValueError("unsupported operator-link protocol version")
        if not self.status_id.strip() or not self.mission_id.strip():
            raise ValueError("status and mission IDs cannot be empty")
        if not 0 <= self.sequence <= 0xFFFFFFFF:
            raise ValueError("sequence must fit in an unsigned 32-bit integer")
        if not isinstance(self.phase, MissionPhase):
            raise ValueError("mission phase is invalid")
        if not isinstance(self.authorization_state, AuthorizationDisplayState):
            raise ValueError("authorization display state is invalid")
        if self.release_window is not None and not isinstance(
            self.release_window, DeploymentWindowStatus
        ):
            raise ValueError("release-window status is invalid")
        if self.safety_allowed is not None and not isinstance(self.safety_allowed, bool):
            raise ValueError("safety_allowed must be boolean or None")
        counts = (self.remaining_payload_count, self.total_payload_count)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in counts):
            raise ValueError("payload counts must be integers")
        if not 0 <= self.remaining_payload_count <= self.total_payload_count <= 65535:
            raise ValueError("payload counts must satisfy 0 <= remaining <= total <= 65535")
        if self.target_id is not None and not self.target_id.strip():
            raise ValueError("target_id cannot be empty when supplied")
        if self.active_payload_slot_id is not None and not self.active_payload_slot_id.strip():
            raise ValueError("active_payload_slot_id cannot be empty when supplied")
        if self.target_confidence is not None and (
            not isfinite(self.target_confidence) or not 0.0 <= self.target_confidence <= 1.0
        ):
            raise ValueError("target_confidence must be in [0, 1]")
        if self.relative_bearing_deg is not None and (
            not isfinite(self.relative_bearing_deg)
            or not -180.0 <= self.relative_bearing_deg <= 180.0
        ):
            raise ValueError("relative_bearing_deg must be in [-180, 180]")
        for name, value in (
            ("estimated_range_m", self.estimated_range_m),
            ("release_lead_distance_m", self.release_lead_distance_m),
        ):
            if value is not None and (not isfinite(value) or value < 0.0):
                raise ValueError(f"{name} must be finite and non-negative")
        for name, value in (
            ("cross_track_error_m", self.cross_track_error_m),
            ("along_track_error_m", self.along_track_error_m),
        ):
            if value is not None and not isfinite(value):
                raise ValueError(f"{name} must be finite when supplied")
        if not isfinite(self.produced_at_s) or self.produced_at_s < 0.0:
            raise ValueError("produced_at_s must be finite and non-negative")
        if not self.advisory_only or self.flight_control_enabled or self.physical_release_enabled:
            raise ValueError("mission status transport must remain display-only")


@dataclass(frozen=True, slots=True)
class PatrolStatusMessage:
    """Mode-1 target-pool and return-observe metadata for local QGC rendering."""

    status_id: str
    sequence: int
    mission_id: str
    phase: PatrolPhase
    primary_target_id: str | None
    target_state: UnifiedTrackState | None
    bbox: BoundingBox | None
    label: str | None
    confidence: float | None
    tracking_quality: float | None
    total_track_count: int
    locked_track_count: int
    source_frame_id: str
    source_captured_at_s: float
    produced_at_s: float
    return_direction: ReturnObserveDirection | None = None
    return_validity: AdvisoryValidity | None = None
    return_evidence_age_s: float | None = None
    estimated_minimum_turn_radius_m: float | None = None
    operator_confirmation_required: bool = True
    sitl_validation_required: bool = True
    advisory_only: bool = True
    flight_control_enabled: bool = False
    protocol_version: int = OPERATOR_LINK_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != OPERATOR_LINK_PROTOCOL_VERSION:
            raise ValueError("unsupported operator-link protocol version")
        if not self.status_id.strip() or not self.mission_id.strip():
            raise ValueError("patrol status and mission IDs cannot be empty")
        if not 0 <= self.sequence <= 0xFFFFFFFF:
            raise ValueError("sequence must fit in an unsigned 32-bit integer")
        if not isinstance(self.phase, PatrolPhase):
            raise ValueError("patrol phase is invalid")
        if self.primary_target_id is not None and not self.primary_target_id.strip():
            raise ValueError("primary_target_id cannot be empty when supplied")
        if self.target_state is not None and not isinstance(self.target_state, UnifiedTrackState):
            raise ValueError("unified target state is invalid")
        if self.label is not None and not self.label.strip():
            raise ValueError("patrol target label cannot be empty when supplied")
        for name, value in (
            ("confidence", self.confidence),
            ("tracking_quality", self.tracking_quality),
        ):
            if value is not None and (not isfinite(value) or not 0.0 <= value <= 1.0):
                raise ValueError(f"{name} must be in [0, 1]")
        counts = (self.total_track_count, self.locked_track_count)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in counts):
            raise ValueError("patrol target counts must be integers")
        if not 0 <= self.locked_track_count <= self.total_track_count <= 65535:
            raise ValueError("patrol target counts must satisfy 0 <= locked <= total <= 65535")
        if not self.source_frame_id.strip():
            raise ValueError("patrol source_frame_id cannot be empty")
        if not all(
            isfinite(value) and value >= 0.0
            for value in (self.source_captured_at_s, self.produced_at_s)
        ):
            raise ValueError("patrol status timestamps must be finite and non-negative")
        if self.produced_at_s < self.source_captured_at_s:
            raise ValueError("patrol status cannot predate its source frame")
        return_fields = (
            self.return_direction,
            self.return_validity,
            self.return_evidence_age_s,
        )
        if any(value is not None for value in return_fields) and not all(
            value is not None for value in return_fields
        ):
            raise ValueError("return-observe direction, validity and evidence age are atomic")
        if self.return_evidence_age_s is not None and (
            not isfinite(self.return_evidence_age_s) or self.return_evidence_age_s < 0.0
        ):
            raise ValueError("return_evidence_age_s must be finite and non-negative")
        if self.estimated_minimum_turn_radius_m is not None and (
            self.return_direction is None
            or not isfinite(self.estimated_minimum_turn_radius_m)
            or self.estimated_minimum_turn_radius_m <= 0.0
        ):
            raise ValueError("turn radius requires finite positive return-observe advice")
        if self.primary_target_id is None and any(
            value is not None
            for value in (
                self.target_state,
                self.bbox,
                self.label,
                self.confidence,
                self.tracking_quality,
                self.return_direction,
            )
        ):
            raise ValueError("target metadata requires a primary target")
        if self.phase is PatrolPhase.PATROL and self.primary_target_id is not None:
            raise ValueError("PATROL status cannot contain a primary target")
        if self.return_direction is not None and self.phase is not PatrolPhase.LOST:
            raise ValueError("return-observe metadata requires LOST patrol phase")
        if (
            not self.operator_confirmation_required
            or not self.sitl_validation_required
            or not self.advisory_only
            or self.flight_control_enabled
        ):
            raise ValueError("patrol status transport must remain confirmed SITL-only advice")


@dataclass(frozen=True, slots=True)
class TargetPoolEntry:
    """One compact read-only background-track summary for paged QGC rendering."""

    target_id: str
    state: UnifiedTrackState
    label: str
    confidence: float
    tracking_quality: float
    locked: bool
    primary: bool
    actionable: bool
    reid_confirmed: bool
    operator_tracked: bool = False
    bbox: BoundingBox | None = None
    relative_bearing_deg: float | None = None
    estimated_range_m: float | None = None
    target_speed_mps: float | None = None

    def __post_init__(self) -> None:
        if not self.target_id.strip() or not self.label.strip():
            raise ValueError("target-pool entry identifiers cannot be empty")
        if not isinstance(self.state, UnifiedTrackState):
            raise ValueError("target-pool entry state is invalid")
        for name, value in (
            ("confidence", self.confidence),
            ("tracking_quality", self.tracking_quality),
        ):
            if not isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"target-pool entry {name} must be in [0, 1]")
        if self.primary and not self.locked:
            raise ValueError("target-pool primary entry must also be locked")
        if self.bbox is not None and not isinstance(self.bbox, BoundingBox):
            raise ValueError("target-pool entry bbox is invalid")
        if self.relative_bearing_deg is not None and (
            not isfinite(self.relative_bearing_deg)
            or not -180.0 <= self.relative_bearing_deg <= 180.0
        ):
            raise ValueError("target-pool relative bearing must be in [-180, 180]")
        for name, value in (
            ("estimated range", self.estimated_range_m),
            ("target speed", self.target_speed_mps),
        ):
            if value is not None and (not isfinite(value) or not 0.0 <= value <= 6_553.4):
                raise ValueError(
                    f"target-pool {name} must be finite and representable in decimetres"
                )


@dataclass(frozen=True, slots=True)
class TargetPoolStatusMessage:
    """A two-entry page from the unified target pool; metadata only."""

    sequence: int
    pool_revision: int
    page_index: int
    page_count: int
    total_track_count: int
    entries: tuple[TargetPoolEntry, ...]
    produced_at_s: float
    advisory_only: bool = True
    flight_control_enabled: bool = False
    physical_release_enabled: bool = False
    protocol_version: int = OPERATOR_LINK_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != OPERATOR_LINK_PROTOCOL_VERSION:
            raise ValueError("unsupported operator-link protocol version")
        for value, bits, name in (
            (self.sequence, 32, "target-pool sequence"),
            (self.pool_revision, 32, "target-pool revision"),
            (self.page_index, 8, "target-pool page index"),
            (self.page_count, 8, "target-pool page count"),
            (self.total_track_count, 8, "target-pool total count"),
        ):
            _require_uint(value, bits=bits, name=name)
        if self.page_count == 0 or self.page_index >= self.page_count:
            raise ValueError("target-pool page coordinates are invalid")
        if self.total_track_count == 0:
            if self.page_count != 1 or self.page_index != 0 or self.entries:
                raise ValueError("empty target-pool status must be one empty page")
        else:
            if not 1 <= len(self.entries) <= 2:
                raise ValueError("target-pool wire page requires one or two entries")
            if self.total_track_count < len(self.entries):
                raise ValueError("target-pool total count is inconsistent")
            if self.page_count != (self.total_track_count + 1) // 2:
                raise ValueError("target-pool page count is inconsistent with the total")
            expected_entries = min(2, self.total_track_count - self.page_index * 2)
            if len(self.entries) != expected_entries:
                raise ValueError("target-pool page entry count is inconsistent")
            if len({entry.target_id for entry in self.entries}) != len(self.entries):
                raise ValueError("target-pool page contains duplicate target IDs")
        if not isfinite(self.produced_at_s) or self.produced_at_s < 0.0:
            raise ValueError("target-pool produced timestamp is invalid")
        if not self.advisory_only or self.flight_control_enabled or self.physical_release_enabled:
            raise ValueError("target-pool status must remain display-only")


class SceneContextState(StrEnum):
    """Freshness state for categorical road/building context shown in QGC."""

    VALID = "VALID"
    INVALID = "INVALID"
    STALE = "STALE"


@dataclass(frozen=True, slots=True)
class SceneContextRegionEntry:
    """One categorical region; confidence is intentionally unavailable."""

    label: str
    bbox: BoundingBox
    frame_area_fraction: float
    bbox_fill_fraction: float
    categorical_mask_only: bool = True
    advisory_only: bool = True
    flight_control_enabled: bool = False
    physical_release_enabled: bool = False

    def __post_init__(self) -> None:
        if self.label not in {"road", "building"}:
            raise ValueError("scene-context wire labels are limited to road and building")
        for name, value in (
            ("frame_area_fraction", self.frame_area_fraction),
            ("bbox_fill_fraction", self.bbox_fill_fraction),
        ):
            if not isfinite(value) or not 0.0 < value <= 1.0:
                raise ValueError(f"scene-context {name} must be in (0, 1]")
        if (
            not self.categorical_mask_only
            or not self.advisory_only
            or self.flight_control_enabled
            or self.physical_release_enabled
        ):
            raise ValueError("scene-context regions must remain categorical display-only metadata")


@dataclass(frozen=True, slots=True)
class SceneContextStatusMessage:
    """An atomic two-entry page of low-rate scene context for QGC rendering."""

    sequence: int
    context_revision: int
    source_frame_id: str
    source_captured_at_s: float
    state: SceneContextState
    page_index: int
    page_count: int
    total_region_count: int
    entries: tuple[SceneContextRegionEntry, ...]
    produced_at_s: float
    confidence_available: bool = False
    target_identity_authority: bool = False
    advisory_only: bool = True
    flight_control_enabled: bool = False
    physical_release_enabled: bool = False
    protocol_version: int = OPERATOR_LINK_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != OPERATOR_LINK_PROTOCOL_VERSION:
            raise ValueError("unsupported operator-link protocol version")
        for value, bits, name in (
            (self.sequence, 32, "scene-context sequence"),
            (self.context_revision, 32, "scene-context revision"),
            (self.page_index, 8, "scene-context page index"),
            (self.page_count, 8, "scene-context page count"),
            (self.total_region_count, 8, "scene-context total count"),
        ):
            _require_uint(value, bits=bits, name=name)
        if not self.source_frame_id.strip():
            raise ValueError("scene-context source frame ID cannot be empty")
        if not isinstance(self.state, SceneContextState):
            raise ValueError("scene-context state is invalid")
        if not all(
            isfinite(value) and value >= 0.0
            for value in (self.source_captured_at_s, self.produced_at_s)
        ):
            raise ValueError("scene-context timestamps must be finite and non-negative")
        if self.produced_at_s < self.source_captured_at_s:
            raise ValueError("scene-context status cannot predate its source frame")
        if self.page_count == 0 or self.page_index >= self.page_count:
            raise ValueError("scene-context page coordinates are invalid")
        if self.state is not SceneContextState.VALID:
            if (
                self.page_index != 0
                or self.page_count != 1
                or self.total_region_count
                or self.entries
            ):
                raise ValueError("invalid or stale scene context must be one empty page")
        elif self.total_region_count == 0:
            if self.page_index != 0 or self.page_count != 1 or self.entries:
                raise ValueError("empty valid scene context must be one empty page")
        else:
            if not 1 <= len(self.entries) <= 2:
                raise ValueError("scene-context wire page requires one or two entries")
            if self.page_count != (self.total_region_count + 1) // 2:
                raise ValueError("scene-context page count is inconsistent with the total")
            expected_entries = min(2, self.total_region_count - self.page_index * 2)
            if len(self.entries) != expected_entries:
                raise ValueError("scene-context page entry count is inconsistent")
        if (
            self.confidence_available
            or self.target_identity_authority
            or not self.advisory_only
            or self.flight_control_enabled
            or self.physical_release_enabled
        ):
            raise ValueError("scene-context status must remain confidence-free display metadata")


@dataclass(frozen=True, slots=True)
class RangeStatusMessage:
    """Read-only multimodal distance metadata for the active primary target."""

    status_id: str
    sequence: int
    target_id: str
    calibration_id: str
    source_frame_id: str
    source_captured_at_s: float
    produced_at_s: float
    validity: RangeValidity
    reasons: tuple[str, ...]
    sources: tuple[str, ...]
    rejected_sources: tuple[str, ...]
    slant_range_m: float | None = None
    ground_range_m: float | None = None
    slant_range_ci95_m: tuple[float, float] | None = None
    ground_range_ci95_m: tuple[float, float] | None = None
    relative_bearing_deg: float | None = None
    absolute_bearing_deg: float | None = None
    bearing_sigma_deg: float | None = None
    north_offset_m: float | None = None
    east_offset_m: float | None = None
    data_freshness_s: float | None = None
    sensor_consistency: float = 0.0
    source_contributions: tuple[RangeSourceContribution, ...] = ()
    fusion_profile: str = "outdoor-multimodal-v1"
    vehicle_profile: str = "auto"
    navigation_state: str = "unknown"
    motion_regime: str = "unknown"
    advisory_only: bool = True
    flight_control_enabled: bool = False
    physical_release_enabled: bool = False
    protocol_version: int = OPERATOR_LINK_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != OPERATOR_LINK_PROTOCOL_VERSION:
            raise ValueError("unsupported operator-link protocol version")
        for value, name in (
            (self.status_id, "status_id"),
            (self.target_id, "target_id"),
            (self.calibration_id, "calibration_id"),
            (self.source_frame_id, "source_frame_id"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"ranging {name} cannot be empty")
        if not 0 <= self.sequence <= 0xFFFFFFFF:
            raise ValueError("sequence must fit in an unsigned 32-bit integer")
        if not isinstance(self.validity, RangeValidity):
            raise ValueError("ranging validity is invalid")
        if not self.reasons or any(reason not in RANGING_REASON_IDS for reason in self.reasons):
            raise ValueError("ranging reasons must use the registered wire vocabulary")
        if any(source not in RANGING_SOURCE_IDS for source in self.sources):
            raise ValueError("ranging sources must use the registered wire vocabulary")
        if any(source not in RANGING_SOURCE_IDS for source in self.rejected_sources):
            raise ValueError("rejected ranging sources must use the registered wire vocabulary")
        if len(set(self.sources)) != len(self.sources) or len(set(self.rejected_sources)) != len(
            self.rejected_sources
        ):
            raise ValueError("ranging source lists cannot contain duplicates")
        if set(self.sources) & set(self.rejected_sources):
            raise ValueError("accepted and rejected ranging sources must be disjoint")
        if not all(
            isfinite(value) and value >= 0.0
            for value in (self.source_captured_at_s, self.produced_at_s)
        ):
            raise ValueError("ranging timestamps must be finite and non-negative")
        if self.produced_at_s < self.source_captured_at_s:
            raise ValueError("ranging status cannot predate its source frame")
        for name, value in (
            ("slant_range_m", self.slant_range_m),
            ("ground_range_m", self.ground_range_m),
            ("bearing_sigma_deg", self.bearing_sigma_deg),
            ("data_freshness_s", self.data_freshness_s),
        ):
            if value is not None and (not isfinite(value) or value < 0.0):
                raise ValueError(f"{name} must be finite and non-negative")
        for name, value in (
            ("relative_bearing_deg", self.relative_bearing_deg),
            ("north_offset_m", self.north_offset_m),
            ("east_offset_m", self.east_offset_m),
        ):
            if value is not None and not isfinite(value):
                raise ValueError(f"{name} must be finite when supplied")
        if self.relative_bearing_deg is not None and not (
            -180.0 <= self.relative_bearing_deg <= 180.0
        ):
            raise ValueError("relative_bearing_deg must be in [-180, 180]")
        if self.absolute_bearing_deg is not None and (
            not isfinite(self.absolute_bearing_deg) or not 0.0 <= self.absolute_bearing_deg < 360.0
        ):
            raise ValueError("absolute_bearing_deg must be in [0, 360)")
        for interval in (self.slant_range_ci95_m, self.ground_range_ci95_m):
            if interval is not None and (
                len(interval) != 2
                or not all(isfinite(value) and value >= 0.0 for value in interval)
                or interval[1] < interval[0]
            ):
                raise ValueError("ranging confidence interval is invalid")
        distance_values = (
            self.slant_range_m,
            self.ground_range_m,
            self.slant_range_ci95_m,
            self.ground_range_ci95_m,
        )
        if self.validity is RangeValidity.INVALID and any(
            value is not None for value in distance_values
        ):
            raise ValueError("invalid ranging status cannot publish distance")
        if not isfinite(self.sensor_consistency) or not 0.0 <= self.sensor_consistency <= 1.0:
            raise ValueError("sensor_consistency must be in [0, 1]")
        if len(self.source_contributions) > 3 or any(
            not isinstance(contribution, RangeSourceContribution)
            or contribution.source not in RANGING_SOURCE_IDS
            for contribution in self.source_contributions
        ):
            raise ValueError("ranging source contributions must contain at most three wire sources")
        if len({contribution.source for contribution in self.source_contributions}) != len(
            self.source_contributions
        ):
            raise ValueError("ranging source contributions cannot contain duplicate sources")
        if sum(contribution.weight for contribution in self.source_contributions) > 1.001:
            raise ValueError("ranging source contribution weights cannot exceed one")
        if self.vehicle_profile not in {"auto", "fixed-wing", "multirotor"}:
            raise ValueError("ranging vehicle profile is invalid")
        if self.navigation_state not in {
            "unknown",
            "vision-only",
            "gps-aided",
            "local-ned",
            "airspeed-dr",
        }:
            raise ValueError("ranging navigation state is invalid")
        if self.motion_regime not in {"unknown", "static", "low-speed", "cruise", "high-speed"}:
            raise ValueError("ranging motion regime is invalid")
        if not self.fusion_profile.strip():
            raise ValueError("ranging fusion profile cannot be empty")
        if not self.advisory_only or self.flight_control_enabled or self.physical_release_enabled:
            raise ValueError("ranging status transport must remain display-only")


@dataclass(frozen=True, slots=True)
class ReleaseStatusMessage:
    """Authenticated Mode-2 impact advice; never an authorization or actuator command."""

    sequence: int
    target_id: str
    calibration_id: str
    produced_at_s: float
    timing_status: ReleaseTimingStatus
    reasons: tuple[str, ...]
    range_target_id: str | None = None
    range_frame_id: str | None = None
    target_north_offset_m: float | None = None
    target_east_offset_m: float | None = None
    impact_north_offset_m: float | None = None
    impact_east_offset_m: float | None = None
    along_track_error_m: float | None = None
    cross_track_error_m: float | None = None
    error_ellipse_major_m: float | None = None
    error_ellipse_minor_m: float | None = None
    error_ellipse_orientation_deg: float | None = None
    estimated_ground_range_m: float | None = None
    ground_range_ci95_m: tuple[float, float] | None = None
    payload_descent_time_s: float | None = None
    release_lead_distance_m: float | None = None
    range_sensor_consistency: float | None = None
    advisory_only: bool = True
    flight_control_enabled: bool = False
    physical_release_enabled: bool = False
    protocol_version: int = OPERATOR_LINK_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != OPERATOR_LINK_PROTOCOL_VERSION:
            raise ValueError("unsupported operator-link protocol version")
        if not 0 <= self.sequence <= 0xFFFFFFFF:
            raise ValueError("sequence must fit in an unsigned 32-bit integer")
        if not self.target_id.strip() or not self.calibration_id.strip():
            raise ValueError("release-status identifiers cannot be empty")
        if not isfinite(self.produced_at_s) or self.produced_at_s < 0.0:
            raise ValueError("release-status timestamp must be finite and non-negative")
        if not isinstance(self.timing_status, ReleaseTimingStatus):
            raise ValueError("release-status timing state is invalid")
        if not self.reasons or any(reason not in RELEASE_REASON_IDS for reason in self.reasons):
            raise ValueError("release-status reasons must use the registered wire vocabulary")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("release-status reasons cannot contain duplicates")
        if (self.range_target_id is None) != (self.range_frame_id is None):
            raise ValueError("release-status range binding must be complete")
        if self.range_target_id is not None and (
            not self.range_target_id.strip()
            or not self.range_frame_id
            or not self.range_frame_id.strip()
        ):
            raise ValueError("release-status range binding identifiers cannot be empty")
        numeric_values = (
            self.target_north_offset_m,
            self.target_east_offset_m,
            self.impact_north_offset_m,
            self.impact_east_offset_m,
            self.along_track_error_m,
            self.cross_track_error_m,
            self.error_ellipse_major_m,
            self.error_ellipse_minor_m,
            self.error_ellipse_orientation_deg,
            self.estimated_ground_range_m,
            self.payload_descent_time_s,
            self.release_lead_distance_m,
            self.range_sensor_consistency,
        )
        if any(value is not None and not isfinite(value) for value in numeric_values):
            raise ValueError("release-status numeric values must be finite when present")
        non_negative = (
            self.error_ellipse_major_m,
            self.error_ellipse_minor_m,
            self.estimated_ground_range_m,
            self.payload_descent_time_s,
            self.release_lead_distance_m,
        )
        if any(value is not None and value < 0.0 for value in non_negative):
            raise ValueError("release-status distances and durations cannot be negative")
        if self.error_ellipse_orientation_deg is not None and not (
            -180.0 <= self.error_ellipse_orientation_deg <= 180.0
        ):
            raise ValueError("release-status ellipse orientation must be in [-180, 180]")
        if self.ground_range_ci95_m is not None and (
            len(self.ground_range_ci95_m) != 2
            or not all(isfinite(value) and value >= 0.0 for value in self.ground_range_ci95_m)
            or self.ground_range_ci95_m[1] < self.ground_range_ci95_m[0]
        ):
            raise ValueError("release-status range confidence interval is invalid")
        if self.range_sensor_consistency is not None and not (
            0.0 <= self.range_sensor_consistency <= 1.0
        ):
            raise ValueError("release-status range consistency must be in [0, 1]")
        complete_geometry = (
            self.range_target_id,
            self.range_frame_id,
            self.target_north_offset_m,
            self.target_east_offset_m,
            self.impact_north_offset_m,
            self.impact_east_offset_m,
            self.along_track_error_m,
            self.cross_track_error_m,
            self.error_ellipse_major_m,
            self.error_ellipse_minor_m,
            self.error_ellipse_orientation_deg,
            self.estimated_ground_range_m,
            self.ground_range_ci95_m,
            self.payload_descent_time_s,
            self.release_lead_distance_m,
            self.range_sensor_consistency,
        )
        if self.timing_status is ReleaseTimingStatus.WINDOW and any(
            value is None for value in complete_geometry
        ):
            raise ValueError("WINDOW release status requires complete bound impact geometry")
        if not self.advisory_only or self.flight_control_enabled or self.physical_release_enabled:
            raise ValueError("release status transport must remain display-only")


@dataclass(frozen=True, slots=True)
class ApproachChallengeStatusMessage:
    """Short-lived Mode-3 slide challenge bound to one selection and target revision."""

    challenge_token: int
    target_token: int
    target_revision: int
    selection_command_id: str
    issued_at_s: float
    expires_at_s: float
    sequence: int
    produced_at_s: float
    pending: bool = True
    sitl_hil_only: bool = True
    flight_control_enabled: bool = False
    protocol_version: int = OPERATOR_LINK_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != OPERATOR_LINK_PROTOCOL_VERSION:
            raise ValueError("unsupported operator-link protocol version")
        _require_uint(self.challenge_token, bits=64, name="approach challenge token", nonzero=True)
        _require_uint(self.target_token, bits=64, name="approach target token", nonzero=True)
        _require_uint(self.target_revision, bits=32, name="approach target revision")
        if not self.selection_command_id.strip():
            raise ValueError("approach selection command ID cannot be empty")
        if not 0 <= self.sequence <= 0xFFFFFFFF:
            raise ValueError("sequence must fit in an unsigned 32-bit integer")
        if not all(
            isfinite(value) and value >= 0.0
            for value in (self.issued_at_s, self.expires_at_s, self.produced_at_s)
        ):
            raise ValueError("approach challenge timestamps are invalid")
        if self.expires_at_s <= self.issued_at_s or self.produced_at_s < self.issued_at_s:
            raise ValueError("approach challenge time ordering is invalid")
        if not self.pending or not self.sitl_hil_only or self.flight_control_enabled:
            raise ValueError("approach challenge must remain pending and HIL-only")


@dataclass(frozen=True, slots=True)
class ApproachConfirmationCommand:
    """Authenticated continuous-slide evidence; it cannot command the aircraft."""

    command_token: int
    session_token: int
    challenge_token: int
    target_token: int
    target_revision: int
    selection_command_id: str
    sequence: int
    issued_at_s: float
    expires_at_s: float
    slide_duration_s: float
    completion_fraction: float
    continuous: bool
    sitl_hil_only: bool = True
    flight_control_enabled: bool = False
    protocol_version: int = OPERATOR_LINK_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != OPERATOR_LINK_PROTOCOL_VERSION:
            raise ValueError("unsupported operator-link protocol version")
        for value, name in (
            (self.command_token, "approach command token"),
            (self.session_token, "approach session token"),
            (self.challenge_token, "approach challenge token"),
            (self.target_token, "approach target token"),
        ):
            _require_uint(value, bits=64, name=name, nonzero=True)
        _require_uint(self.target_revision, bits=32, name="approach target revision")
        if not self.selection_command_id.strip():
            raise ValueError("approach selection command ID cannot be empty")
        if not 0 <= self.sequence <= 0xFFFFFFFF:
            raise ValueError("sequence must fit in an unsigned 32-bit integer")
        if not all(isfinite(value) for value in (self.issued_at_s, self.expires_at_s)):
            raise ValueError("approach confirmation timestamps must be finite")
        ttl_s = self.expires_at_s - self.issued_at_s
        if ttl_s <= 0.0 or ttl_s > MAX_SELECTION_TTL_S:
            raise ValueError("approach confirmation TTL is invalid")
        if not isfinite(self.slide_duration_s) or not 0.0 < self.slide_duration_s <= 10.0:
            raise ValueError("approach slide duration is invalid")
        if not isfinite(self.completion_fraction) or not 0.0 <= self.completion_fraction <= 1.0:
            raise ValueError("approach slide completion must be in [0, 1]")
        if not isinstance(self.continuous, bool):
            raise ValueError("approach continuous flag must be boolean")
        if not self.sitl_hil_only or self.flight_control_enabled:
            raise ValueError("approach confirmation must remain HIL-only")


@dataclass(frozen=True, slots=True)
class ApproachConfirmationAcceptance:
    allowed: bool
    reasons: tuple[str, ...]
    duplicate: bool = False


class ApproachConfirmationCommandGuard:
    """Reject clicks, replay, stale data and any command not bound to the active challenge."""

    def __init__(
        self,
        *,
        minimum_slide_duration_s: float = 0.6,
        maximum_slide_duration_s: float = 4.0,
        minimum_completion_fraction: float = 0.98,
        clock_tolerance_s: float = 0.5,
        replay_window_size: int = 256,
    ) -> None:
        if not 0.0 < minimum_slide_duration_s < maximum_slide_duration_s:
            raise ValueError("approach slide duration guard is invalid")
        if not 0.0 < minimum_completion_fraction <= 1.0:
            raise ValueError("approach completion guard must be in (0, 1]")
        if not isfinite(clock_tolerance_s) or clock_tolerance_s < 0.0:
            raise ValueError("approach clock tolerance must be finite and non-negative")
        if replay_window_size <= 0:
            raise ValueError("approach replay window must be positive")
        self.minimum_slide_duration_s = minimum_slide_duration_s
        self.maximum_slide_duration_s = maximum_slide_duration_s
        self.minimum_completion_fraction = minimum_completion_fraction
        self.clock_tolerance_s = clock_tolerance_s
        self._active: ApproachChallengeStatusMessage | None = None
        self._last_sequence_by_session: dict[int, int] = {}
        self._processed: dict[
            int,
            tuple[ApproachConfirmationCommand, ApproachConfirmationAcceptance],
        ] = {}
        self._processed_order: deque[int] = deque(maxlen=replay_window_size)
        self._consumed_challenges: set[int] = set()

    def set_active_challenge(self, challenge: ApproachChallengeStatusMessage | None) -> None:
        self._active = challenge

    def evaluate(
        self,
        command: ApproachConfirmationCommand,
        *,
        received_at_s: float,
    ) -> ApproachConfirmationAcceptance:
        if not isfinite(received_at_s) or received_at_s < 0.0:
            raise ValueError("approach receipt timestamp must be finite and non-negative")
        cached = self._processed.get(command.command_token)
        if cached is not None:
            previous, acceptance = cached
            if previous == command:
                return ApproachConfirmationAcceptance(
                    acceptance.allowed,
                    acceptance.reasons,
                    duplicate=True,
                )
            return ApproachConfirmationAcceptance(
                False,
                ("approach command token was reused with different content",),
            )

        reasons: list[str] = []
        active = self._active
        if active is None:
            reasons.append("no active approach challenge")
        else:
            if (
                command.challenge_token != active.challenge_token
                or command.target_token != active.target_token
                or command.target_revision != active.target_revision
                or command.selection_command_id != active.selection_command_id
            ):
                reasons.append("approach command does not match the active challenge")
            if received_at_s >= active.expires_at_s:
                reasons.append("approach challenge has expired")
            if command.expires_at_s > active.expires_at_s + self.clock_tolerance_s:
                reasons.append("approach command outlives its challenge")
            if command.challenge_token in self._consumed_challenges:
                reasons.append("approach challenge was already consumed")
        if received_at_s > command.expires_at_s + self.clock_tolerance_s:
            reasons.append("approach command is stale")
        if received_at_s < command.issued_at_s - self.clock_tolerance_s:
            reasons.append("approach command is dated in the future")
        previous_sequence = self._last_sequence_by_session.get(command.session_token)
        if previous_sequence is not None and command.sequence <= previous_sequence:
            reasons.append("approach sequence is not newer than the last command")
        if (
            not command.continuous
            or command.slide_duration_s < self.minimum_slide_duration_s
            or command.slide_duration_s > self.maximum_slide_duration_s
            or command.completion_fraction < self.minimum_completion_fraction
        ):
            reasons.append("approach slide evidence is incomplete")

        acceptance = ApproachConfirmationAcceptance(not reasons, tuple(reasons))
        self._remember(command, acceptance)
        if acceptance.allowed:
            self._last_sequence_by_session[command.session_token] = command.sequence
            self._consumed_challenges.add(command.challenge_token)
        return acceptance

    def _remember(
        self,
        command: ApproachConfirmationCommand,
        acceptance: ApproachConfirmationAcceptance,
    ) -> None:
        if len(self._processed_order) == self._processed_order.maxlen:
            self._processed.pop(self._processed_order[0], None)
        self._processed_order.append(command.command_token)
        self._processed[command.command_token] = (command, acceptance)


@dataclass(frozen=True, slots=True)
class PayloadTargetChallengeStatusMessage:
    """Mode-2 slide challenge bound to the selection and resolved fire aimpoint."""

    challenge_token: int
    selected_target_token: int
    selected_target_revision: int
    aimpoint_target_token: int
    aimpoint_target_revision: int
    selection_command_id: str
    issued_at_s: float
    expires_at_s: float
    sequence: int
    produced_at_s: float
    pending: bool = True
    hil_only: bool = True
    physical_release_enabled: bool = False
    protocol_version: int = OPERATOR_LINK_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != OPERATOR_LINK_PROTOCOL_VERSION:
            raise ValueError("unsupported operator-link protocol version")
        for value, name in (
            (self.challenge_token, "payload target challenge token"),
            (self.selected_target_token, "payload selected target token"),
            (self.aimpoint_target_token, "payload aimpoint target token"),
        ):
            _require_uint(value, bits=64, name=name, nonzero=True)
        _require_uint(
            self.selected_target_revision,
            bits=32,
            name="payload selected target revision",
        )
        _require_uint(
            self.aimpoint_target_revision,
            bits=32,
            name="payload aimpoint target revision",
        )
        if not self.selection_command_id.strip():
            raise ValueError("payload selection command ID cannot be empty")
        _require_uint(self.sequence, bits=32, name="payload target challenge sequence")
        if not all(
            isfinite(value) and value >= 0.0
            for value in (self.issued_at_s, self.expires_at_s, self.produced_at_s)
        ):
            raise ValueError("payload target challenge timestamps are invalid")
        if self.expires_at_s <= self.issued_at_s or self.produced_at_s < self.issued_at_s:
            raise ValueError("payload target challenge time ordering is invalid")
        if not self.pending or not self.hil_only or self.physical_release_enabled:
            raise ValueError("payload target challenge must remain pending and HIL-only")


@dataclass(frozen=True, slots=True)
class PayloadTargetConfirmationCommand:
    """Authenticated Mode-2 continuous-slide evidence; never an actuator command."""

    command_token: int
    session_token: int
    challenge_token: int
    selected_target_token: int
    selected_target_revision: int
    aimpoint_target_token: int
    aimpoint_target_revision: int
    selection_command_id: str
    sequence: int
    issued_at_s: float
    expires_at_s: float
    slide_duration_s: float
    completion_fraction: float
    continuous: bool
    hil_only: bool = True
    physical_release_enabled: bool = False
    protocol_version: int = OPERATOR_LINK_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != OPERATOR_LINK_PROTOCOL_VERSION:
            raise ValueError("unsupported operator-link protocol version")
        for value, name in (
            (self.command_token, "payload target command token"),
            (self.session_token, "payload target session token"),
            (self.challenge_token, "payload target challenge token"),
            (self.selected_target_token, "payload selected target token"),
            (self.aimpoint_target_token, "payload aimpoint target token"),
        ):
            _require_uint(value, bits=64, name=name, nonzero=True)
        _require_uint(
            self.selected_target_revision,
            bits=32,
            name="payload selected target revision",
        )
        _require_uint(
            self.aimpoint_target_revision,
            bits=32,
            name="payload aimpoint target revision",
        )
        if not self.selection_command_id.strip():
            raise ValueError("payload selection command ID cannot be empty")
        _require_uint(self.sequence, bits=32, name="payload confirmation sequence")
        if not all(isfinite(value) for value in (self.issued_at_s, self.expires_at_s)):
            raise ValueError("payload target confirmation timestamps must be finite")
        ttl_s = self.expires_at_s - self.issued_at_s
        if ttl_s <= 0.0 or ttl_s > MAX_SELECTION_TTL_S:
            raise ValueError("payload target confirmation TTL is invalid")
        if not isfinite(self.slide_duration_s) or not 0.0 < self.slide_duration_s <= 10.0:
            raise ValueError("payload target slide duration is invalid")
        if not isfinite(self.completion_fraction) or not 0.0 <= self.completion_fraction <= 1.0:
            raise ValueError("payload target slide completion must be in [0, 1]")
        if not isinstance(self.continuous, bool):
            raise ValueError("payload target continuous flag must be boolean")
        if not self.hil_only or self.physical_release_enabled:
            raise ValueError("payload target confirmation must remain HIL-only")


@dataclass(frozen=True, slots=True)
class PayloadTargetConfirmationAcceptance:
    allowed: bool
    reasons: tuple[str, ...]
    duplicate: bool = False


class PayloadTargetConfirmationCommandGuard:
    """Reject replay, clicks, stale data and either half of a changed target binding."""

    def __init__(
        self,
        *,
        minimum_slide_duration_s: float = 0.6,
        maximum_slide_duration_s: float = 4.0,
        minimum_completion_fraction: float = 0.98,
        clock_tolerance_s: float = 0.5,
        replay_window_size: int = 256,
    ) -> None:
        if not 0.0 < minimum_slide_duration_s < maximum_slide_duration_s:
            raise ValueError("payload target slide duration guard is invalid")
        if not 0.0 < minimum_completion_fraction <= 1.0:
            raise ValueError("payload target completion guard must be in (0, 1]")
        if not isfinite(clock_tolerance_s) or clock_tolerance_s < 0.0:
            raise ValueError("payload target clock tolerance must be finite and non-negative")
        if replay_window_size <= 0:
            raise ValueError("payload target replay window must be positive")
        self.minimum_slide_duration_s = minimum_slide_duration_s
        self.maximum_slide_duration_s = maximum_slide_duration_s
        self.minimum_completion_fraction = minimum_completion_fraction
        self.clock_tolerance_s = clock_tolerance_s
        self._active: PayloadTargetChallengeStatusMessage | None = None
        self._last_sequence_by_session: dict[int, int] = {}
        self._processed: dict[
            int,
            tuple[PayloadTargetConfirmationCommand, PayloadTargetConfirmationAcceptance],
        ] = {}
        self._processed_order: deque[int] = deque(maxlen=replay_window_size)
        self._consumed_challenges: set[int] = set()

    def set_active_challenge(
        self,
        challenge: PayloadTargetChallengeStatusMessage | None,
    ) -> None:
        self._active = challenge

    def evaluate(
        self,
        command: PayloadTargetConfirmationCommand,
        *,
        received_at_s: float,
    ) -> PayloadTargetConfirmationAcceptance:
        if not isfinite(received_at_s) or received_at_s < 0.0:
            raise ValueError("payload target receipt timestamp must be finite and non-negative")
        cached = self._processed.get(command.command_token)
        if cached is not None:
            previous, acceptance = cached
            if previous == command:
                return PayloadTargetConfirmationAcceptance(
                    acceptance.allowed,
                    acceptance.reasons,
                    duplicate=True,
                )
            return PayloadTargetConfirmationAcceptance(
                False,
                ("payload target command token was reused with different content",),
            )

        reasons: list[str] = []
        active = self._active
        if active is None:
            reasons.append("no active payload target challenge")
        else:
            if (
                command.challenge_token != active.challenge_token
                or command.selected_target_token != active.selected_target_token
                or command.selected_target_revision != active.selected_target_revision
                or command.aimpoint_target_token != active.aimpoint_target_token
                or command.aimpoint_target_revision != active.aimpoint_target_revision
                or command.selection_command_id != active.selection_command_id
            ):
                reasons.append("payload target command does not match the active challenge")
            if received_at_s >= active.expires_at_s:
                reasons.append("payload target challenge has expired")
            if command.expires_at_s > active.expires_at_s + self.clock_tolerance_s:
                reasons.append("payload target command outlives its challenge")
            if command.challenge_token in self._consumed_challenges:
                reasons.append("payload target challenge was already consumed")
        if received_at_s > command.expires_at_s + self.clock_tolerance_s:
            reasons.append("payload target command is stale")
        if received_at_s < command.issued_at_s - self.clock_tolerance_s:
            reasons.append("payload target command is dated in the future")
        previous_sequence = self._last_sequence_by_session.get(command.session_token)
        if previous_sequence is not None and command.sequence <= previous_sequence:
            reasons.append("payload target sequence is not newer than the last command")
        if (
            not command.continuous
            or command.slide_duration_s < self.minimum_slide_duration_s
            or command.slide_duration_s > self.maximum_slide_duration_s
            or command.completion_fraction < self.minimum_completion_fraction
        ):
            reasons.append("payload target slide evidence is incomplete")

        acceptance = PayloadTargetConfirmationAcceptance(not reasons, tuple(reasons))
        self._remember(command, acceptance)
        if acceptance.allowed:
            self._last_sequence_by_session[command.session_token] = command.sequence
            self._consumed_challenges.add(command.challenge_token)
        return acceptance

    def _remember(
        self,
        command: PayloadTargetConfirmationCommand,
        acceptance: PayloadTargetConfirmationAcceptance,
    ) -> None:
        if len(self._processed_order) == self._processed_order.maxlen:
            self._processed.pop(self._processed_order[0], None)
        self._processed_order.append(command.command_token)
        self._processed[command.command_token] = (command, acceptance)


@dataclass(frozen=True, slots=True)
class PayloadTargetStatusMessage:
    """Read-only Mode-2 selection eligibility and slide-confirmation state."""

    sequence: int
    selection_command_id: str
    selected_target_token: int
    selected_target_revision: int
    eligibility: PayloadTargetEligibility
    produced_at_s: float
    aimpoint_target_token: int | None = None
    aimpoint_target_revision: int | None = None
    confirmation_pending: bool = False
    confirmation_accepted: bool = False
    confirmation_expires_at_s: float | None = None
    advisory_only: bool = True
    hil_only: bool = True
    flight_control_enabled: bool = False
    physical_release_enabled: bool = False
    protocol_version: int = OPERATOR_LINK_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != OPERATOR_LINK_PROTOCOL_VERSION:
            raise ValueError("unsupported operator-link protocol version")
        _require_uint(self.sequence, bits=32, name="payload target status sequence")
        if not self.selection_command_id.strip():
            raise ValueError("payload target status selection ID cannot be empty")
        _require_uint(
            self.selected_target_token,
            bits=64,
            name="payload target status selected token",
            nonzero=True,
        )
        _require_uint(
            self.selected_target_revision,
            bits=32,
            name="payload target status selected revision",
        )
        if not isinstance(self.eligibility, PayloadTargetEligibility):
            raise ValueError("payload target status eligibility is invalid")
        if not isfinite(self.produced_at_s) or self.produced_at_s < 0.0:
            raise ValueError("payload target status timestamp is invalid")
        aimpoint_atomic = (self.aimpoint_target_token is None) == (
            self.aimpoint_target_revision is None
        )
        if not aimpoint_atomic:
            raise ValueError("payload target status aimpoint binding must be atomic")
        if self.aimpoint_target_token is not None:
            _require_uint(
                self.aimpoint_target_token,
                bits=64,
                name="payload target status aimpoint token",
                nonzero=True,
            )
            _require_uint(
                self.aimpoint_target_revision,
                bits=32,
                name="payload target status aimpoint revision",
            )
        eligible = self.eligibility in {
            PayloadTargetEligibility.ELIGIBLE_FIRE,
            PayloadTargetEligibility.ELIGIBLE_BURNING_CONTEXT,
        }
        if eligible != (self.aimpoint_target_token is not None):
            raise ValueError("payload target status eligibility and aimpoint disagree")
        if self.confirmation_pending and self.confirmation_accepted:
            raise ValueError("payload target confirmation cannot be pending and accepted")
        if (self.confirmation_pending or self.confirmation_accepted) and not eligible:
            raise ValueError("ineligible payload target cannot have confirmation state")
        if (self.confirmation_expires_at_s is not None) != (
            self.confirmation_pending or self.confirmation_accepted
        ):
            raise ValueError("payload target confirmation expiry is inconsistent")
        if self.confirmation_expires_at_s is not None and (
            not isfinite(self.confirmation_expires_at_s)
            or self.confirmation_expires_at_s < self.produced_at_s
        ):
            raise ValueError("payload target confirmation expiry is invalid")
        if (
            not self.advisory_only
            or not self.hil_only
            or self.flight_control_enabled
            or self.physical_release_enabled
        ):
            raise ValueError("payload target status must remain advisory HIL-only")


@dataclass(frozen=True, slots=True)
class ApproachStatusMessage:
    """Mode-3 phase and bounded centering state reported to QGC.

    The packet remains metadata. ``flight_control_enabled`` tells QGC whether the
    Jetson-side fixed-wing controller is the active control authority; QGC never
    duplicates those Pixhawk setpoints.
    """

    sequence: int
    target_id: str | None
    target_revision: int | None
    phase: ApproachHilPhase
    reasons: tuple[str, ...]
    produced_at_s: float
    yaw_error_deg: float | None = None
    pitch_error_deg: float | None = None
    yaw_advice_deg: float | None = None
    pitch_advice_deg: float | None = None
    bank_advice_deg: float | None = None
    climb_pitch_advice_deg: float | None = None
    ground_range_m: float | None = None
    confirmation_expires_at_s: float | None = None
    advisory_only: bool = True
    sitl_hil_only: bool = True
    flight_control_enabled: bool = False
    aim_control_active: bool = False
    pilot_input_cancelled: bool = False
    physical_release_enabled: bool = False
    protocol_version: int = OPERATOR_LINK_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != OPERATOR_LINK_PROTOCOL_VERSION:
            raise ValueError("unsupported operator-link protocol version")
        if not 0 <= self.sequence <= 0xFFFFFFFF:
            raise ValueError("sequence must fit in an unsigned 32-bit integer")
        if (self.target_id is None) != (self.target_revision is None):
            raise ValueError("approach status target binding must be atomic")
        if self.target_id is not None and not self.target_id.strip():
            raise ValueError("approach status target ID cannot be empty")
        if self.target_revision is not None:
            _require_uint(self.target_revision, bits=32, name="approach target revision")
        if not isinstance(self.phase, ApproachHilPhase):
            raise ValueError("approach status phase is invalid")
        if not self.reasons or any(reason not in APPROACH_REASON_IDS for reason in self.reasons):
            raise ValueError("approach reasons must use the registered wire vocabulary")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("approach reasons cannot contain duplicates")
        numeric = (
            self.produced_at_s,
            self.yaw_error_deg,
            self.pitch_error_deg,
            self.yaw_advice_deg,
            self.pitch_advice_deg,
            self.bank_advice_deg,
            self.climb_pitch_advice_deg,
            self.ground_range_m,
            self.confirmation_expires_at_s,
        )
        if any(value is not None and (not isfinite(value) or value < 0.0) for value in numeric[:1]):
            raise ValueError("approach status timestamp is invalid")
        if any(value is not None and not isfinite(value) for value in numeric[1:]):
            raise ValueError("approach status numeric advice must be finite")
        if self.ground_range_m is not None and self.ground_range_m < 0.0:
            raise ValueError("approach ground range cannot be negative")
        if self.confirmation_expires_at_s is not None and (
            self.confirmation_expires_at_s < self.produced_at_s
        ):
            raise ValueError("approach confirmation expiry cannot predate status")
        metadata_mode = (
            self.advisory_only and self.sitl_hil_only and not self.flight_control_enabled
        )
        jetson_control_mode = (
            not self.advisory_only and not self.sitl_hil_only and self.flight_control_enabled
        )
        if (not metadata_mode and not jetson_control_mode) or self.physical_release_enabled:
            raise ValueError("approach status control-authority flags are inconsistent")
        if self.aim_control_active and not self.flight_control_enabled:
            raise ValueError("active aim control requires Jetson flight-control authority")
        if self.pilot_input_cancelled and (
            not self.flight_control_enabled or self.aim_control_active
        ):
            raise ValueError("pilot cancellation flags are inconsistent")


@dataclass(frozen=True, slots=True)
class SafetyStatusMessage:
    """Compact read-only rule verdicts for an explanatory G20 safety panel."""

    status_id: str
    sequence: int
    mission_id: str
    target_id: str
    ruleset_version: str
    checks: tuple[RuleCheck, ...]
    produced_at_s: float
    registry_version: int = SAFETY_RULE_REGISTRY_VERSION
    advisory_only: bool = True
    flight_control_enabled: bool = False
    physical_release_enabled: bool = False
    protocol_version: int = OPERATOR_LINK_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != OPERATOR_LINK_PROTOCOL_VERSION:
            raise ValueError("unsupported operator-link protocol version")
        if self.registry_version != SAFETY_RULE_REGISTRY_VERSION:
            raise ValueError("unsupported safety-rule registry version")
        for value, name in (
            (self.status_id, "status_id"),
            (self.mission_id, "mission_id"),
            (self.target_id, "target_id"),
            (self.ruleset_version, "ruleset_version"),
        ):
            if not value.strip():
                raise ValueError(f"{name} cannot be empty")
        if not 0 <= self.sequence <= 0xFFFFFFFF:
            raise ValueError("sequence must fit in an unsigned 32-bit integer")
        if not self.checks:
            raise ValueError("safety status requires at least one rule check")
        known = set(OPERATOR_SAFETY_RULE_IDS)
        seen: set[str] = set()
        for check in self.checks:
            if not isinstance(check, RuleCheck):
                raise ValueError("safety status checks must be RuleCheck values")
            if check.rule_id not in known:
                raise ValueError(f"safety status rule is not registered: {check.rule_id}")
            if check.rule_id in seen:
                raise ValueError(f"safety status rule is duplicated: {check.rule_id}")
            seen.add(check.rule_id)
        if not isfinite(self.produced_at_s) or self.produced_at_s < 0.0:
            raise ValueError("produced_at_s must be finite and non-negative")
        if not self.advisory_only or self.flight_control_enabled or self.physical_release_enabled:
            raise ValueError("safety status transport must remain display-only")

    @property
    def pass_count(self) -> int:
        return sum(check.verdict is Verdict.PASS for check in self.checks)

    @property
    def deny_count(self) -> int:
        return sum(check.verdict is Verdict.DENY for check in self.checks)

    @property
    def unknown_count(self) -> int:
        return sum(check.verdict is Verdict.UNKNOWN for check in self.checks)

    @property
    def allowed(self) -> bool:
        return bool(self.checks) and self.pass_count == len(self.checks)


__all__ = [
    "AuthorizationChallengeStatusMessage",
    "APPROACH_REASON_IDS",
    "ApproachChallengeStatusMessage",
    "ApproachConfirmationCommand",
    "ApproachConfirmationAcceptance",
    "ApproachConfirmationCommandGuard",
    "ApproachStatusMessage",
    "AuthorizationDecision",
    "AuthorizationDecisionAcceptance",
    "AuthorizationDecisionCommand",
    "AuthorizationDecisionCommandGuard",
    "AuthorizationDisplayState",
    "MAX_AUTHORIZATION_DECISION_TTL_S",
    "MAX_SELECTION_TTL_S",
    "MissionStatusMessage",
    "OPERATOR_SAFETY_RULE_IDS",
    "OPERATOR_LINK_PROTOCOL_VERSION",
    "RANGING_REASON_IDS",
    "RANGING_SOURCE_IDS",
    "RELEASE_REASON_IDS",
    "RangeStatusMessage",
    "ReleaseStatusMessage",
    "SceneContextRegionEntry",
    "SceneContextState",
    "SceneContextStatusMessage",
    "SAFETY_RULE_REGISTRY_VERSION",
    "PatrolStatusMessage",
    "PayloadTargetChallengeStatusMessage",
    "PayloadTargetConfirmationAcceptance",
    "PayloadTargetConfirmationCommand",
    "PayloadTargetConfirmationCommandGuard",
    "PayloadTargetStatusMessage",
    "SafetyStatusMessage",
    "SelectionAcceptance",
    "SelectionAction",
    "SelectionCommandGuard",
    "TargetSelectionCommand",
    "TargetPoolEntry",
    "TargetPoolStatusMessage",
    "TrackingState",
    "TrackStatusMessage",
    "VideoGeometry",
    "operator_identifier_token",
]
