from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite

from .domain import BoundingBox, DeploymentWindowStatus, MissionPhase, RuleCheck, Verdict

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
    "sensor.thermal_consistency",
)

if len(OPERATOR_SAFETY_RULE_IDS) > 32:
    raise RuntimeError("operator safety rule registry exceeds the 32-bit wire mask")
if len(set(OPERATOR_SAFETY_RULE_IDS)) != len(OPERATOR_SAFETY_RULE_IDS):
    raise RuntimeError("operator safety rule registry contains duplicate rule IDs")


class SelectionAction(StrEnum):
    SELECT = "select"
    SWITCH = "switch"
    CANCEL = "cancel"


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
    "SAFETY_RULE_REGISTRY_VERSION",
    "SafetyStatusMessage",
    "SelectionAcceptance",
    "SelectionAction",
    "SelectionCommandGuard",
    "TargetSelectionCommand",
    "TrackingState",
    "TrackStatusMessage",
    "VideoGeometry",
    "operator_identifier_token",
]
