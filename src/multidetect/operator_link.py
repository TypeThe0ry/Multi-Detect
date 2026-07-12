from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite

from .domain import BoundingBox

OPERATOR_LINK_PROTOCOL_VERSION = 1
MAX_SELECTION_TTL_S = 5.0


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


__all__ = [
    "MAX_SELECTION_TTL_S",
    "OPERATOR_LINK_PROTOCOL_VERSION",
    "SelectionAcceptance",
    "SelectionAction",
    "SelectionCommandGuard",
    "TargetSelectionCommand",
    "TrackingState",
    "TrackStatusMessage",
    "VideoGeometry",
]
