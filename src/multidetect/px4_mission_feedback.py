"""Hash-bound, receive-only PX4 mission execution feedback.

``MISSION_CURRENT`` and ``MISSION_ITEM_REACHED`` are the autopilot's own
observation of Mission execution.  They are deliberately kept separate from
the geometric estimator in :mod:`multidetect.mission_progress`: a wire event
can gate or corroborate progress, but it must never create a flight command.

The tracker binds every accepted event to one :class:`ExecutableMission`, one
MAVLink source identity, a monotonic receive clock and a bounded freshness
window.  Sequence regressions, out-of-range items, source changes and
out-of-order receive timestamps latch the snapshot invalid until an explicit
reset.  That makes the same object suitable for SIM, HIL and REAL evidence
without silently treating a stale mission cursor as current execution.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from numbers import Real

from .autonomy.contracts import ExecutableMission
from .autonomy.serialization import (
    AutonomyContractError,
    canonical_sha256,
    optional_real,
    require_array,
    require_bool,
    require_int,
    require_real,
    require_schema_version,
    require_sha256,
    require_text,
    strict_object,
)
from .compat import StrEnum

PX4_MISSION_FEEDBACK_SCHEMA_VERSION = 1
PX4_MISSION_FEEDBACK_CANONICAL_MAX_AGE_S = 1.5
PX4_MISSION_FEEDBACK_MAX_ITEMS = 64
_MISSION_MESSAGE_TYPES = frozenset({"MISSION_CURRENT", "MISSION_ITEM_REACHED"})


class Px4MissionFeedbackError(AutonomyContractError):
    """Raised when a feedback contract or snapshot is internally invalid."""


class Px4MissionFeedbackPhase(StrEnum):
    """Fail-closed lifecycle of the receive-only Mission feedback stream."""

    AWAITING = "awaiting"
    ACTIVE = "active"
    TERMINAL_REACHED = "terminal_reached"
    STALE = "stale"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class Px4MissionFeedbackConfig:
    """Source identity and freshness policy for one Mission feedback stream."""

    maximum_age_s: float = PX4_MISSION_FEEDBACK_CANONICAL_MAX_AGE_S
    expected_system_id: int | None = None
    expected_component_id: int | None = None

    def __post_init__(self) -> None:
        maximum_age = _finite(self.maximum_age_s, "maximum_age_s")
        if maximum_age <= 0.0:
            raise Px4MissionFeedbackError("maximum_age_s must be greater than zero")
        if maximum_age > PX4_MISSION_FEEDBACK_CANONICAL_MAX_AGE_S:
            raise Px4MissionFeedbackError(
                "maximum_age_s cannot exceed the canonical Mission feedback freshness budget"
            )
        object.__setattr__(self, "maximum_age_s", maximum_age)
        for name in ("expected_system_id", "expected_component_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _mavlink_source_id(value, name))
        if self.expected_component_id is not None and self.expected_system_id is None:
            raise Px4MissionFeedbackError("expected_component_id requires expected_system_id")


@dataclass(frozen=True, slots=True)
class Px4MissionFeedbackSnapshot:
    """Canonical evidence snapshot for one bound PX4 Mission feedback stream."""

    mission_id: str
    mission_hash: str
    mission_item_count: int
    evaluated_at_s: float
    maximum_age_s: float
    phase: Px4MissionFeedbackPhase
    observed_current_sequences: tuple[int, ...]
    current_sequence: int | None
    current_observed_at_s: float | None
    reached_sequences: tuple[int, ...]
    last_reached_observed_at_s: float | None
    last_event_observed_at_s: float | None
    feedback_age_s: float | None
    expected_system_id: int | None
    expected_component_id: int | None
    source_system_id: int | None
    source_component_id: int | None
    source_identity_bound: bool
    accepted_event_count: int
    duplicate_event_count: int
    rejected_event_count: int
    last_rejection_reason: str | None
    qualified: bool
    reason: str
    schema_version: int = PX4_MISSION_FEEDBACK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", require_schema_version(self.schema_version))
        object.__setattr__(self, "mission_id", require_text(self.mission_id, "mission_id"))
        object.__setattr__(self, "mission_hash", require_sha256(self.mission_hash, "mission_hash"))
        count = require_int(
            self.mission_item_count,
            "mission_item_count",
            minimum=1,
            maximum=PX4_MISSION_FEEDBACK_MAX_ITEMS,
        )
        object.__setattr__(self, "mission_item_count", count)
        evaluated = require_real(self.evaluated_at_s, "evaluated_at_s", minimum=0.0)
        maximum_age = require_real(
            self.maximum_age_s,
            "maximum_age_s",
            minimum=0.0,
            maximum=PX4_MISSION_FEEDBACK_CANONICAL_MAX_AGE_S,
        )
        if maximum_age <= 0.0:
            raise Px4MissionFeedbackError("maximum_age_s must be greater than zero")
        object.__setattr__(self, "evaluated_at_s", evaluated)
        object.__setattr__(self, "maximum_age_s", maximum_age)
        if not isinstance(self.phase, Px4MissionFeedbackPhase):
            raise Px4MissionFeedbackError("phase must be a Px4MissionFeedbackPhase")

        current_history = _sequence_history(
            self.observed_current_sequences,
            "observed_current_sequences",
            item_count=count,
        )
        reached = _sequence_history(
            self.reached_sequences,
            "reached_sequences",
            item_count=count,
        )
        object.__setattr__(self, "observed_current_sequences", current_history)
        object.__setattr__(self, "reached_sequences", reached)
        current = _optional_sequence(self.current_sequence, "current_sequence", count)
        object.__setattr__(self, "current_sequence", current)
        if current_history:
            if current != current_history[-1]:
                raise Px4MissionFeedbackError(
                    "current_sequence must equal the last observed current sequence"
                )
        elif current is not None:
            raise Px4MissionFeedbackError("current_sequence requires observed_current_sequences")

        for name in (
            "current_observed_at_s",
            "last_reached_observed_at_s",
            "last_event_observed_at_s",
            "feedback_age_s",
        ):
            object.__setattr__(
                self,
                name,
                optional_real(getattr(self, name), name, minimum=0.0),
            )
        if (current is None) != (self.current_observed_at_s is None):
            raise Px4MissionFeedbackError("current sequence and timestamp must be present together")
        if bool(reached) != (self.last_reached_observed_at_s is not None):
            raise Px4MissionFeedbackError(
                "reached sequences and timestamp must be present together"
            )
        has_events = bool(current_history or reached)
        if has_events != (self.last_event_observed_at_s is not None):
            raise Px4MissionFeedbackError(
                "accepted feedback and last-event timestamp must be present together"
            )
        if has_events != (self.feedback_age_s is not None):
            raise Px4MissionFeedbackError(
                "accepted feedback and feedback age must be present together"
            )
        if self.last_event_observed_at_s is not None:
            if self.last_event_observed_at_s > evaluated:
                raise Px4MissionFeedbackError("last_event_observed_at_s cannot be in the future")
            expected_age = evaluated - self.last_event_observed_at_s
            if not math.isclose(
                float(self.feedback_age_s), expected_age, rel_tol=0.0, abs_tol=1.0e-9
            ):
                raise Px4MissionFeedbackError(
                    "feedback_age_s does not match the last event timestamp"
                )
        for timestamp_name in (
            "current_observed_at_s",
            "last_reached_observed_at_s",
        ):
            timestamp = getattr(self, timestamp_name)
            if timestamp is not None and timestamp > evaluated:
                raise Px4MissionFeedbackError(f"{timestamp_name} cannot be in the future")

        for name in (
            "expected_system_id",
            "expected_component_id",
            "source_system_id",
            "source_component_id",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _mavlink_source_id(value, name))
        if self.expected_component_id is not None and self.expected_system_id is None:
            raise Px4MissionFeedbackError("expected_component_id requires expected_system_id")
        if (self.source_system_id is None) != (self.source_component_id is None):
            raise Px4MissionFeedbackError(
                "source system and component IDs must be present together"
            )
        identity_bound = self.source_system_id is not None
        if self.source_identity_bound is not identity_bound:
            raise Px4MissionFeedbackError(
                "source_identity_bound does not match the observed identity"
            )
        if identity_bound:
            if (
                self.expected_system_id is not None
                and self.source_system_id != self.expected_system_id
            ):
                raise Px4MissionFeedbackError("source system does not match its expected identity")
            if (
                self.expected_component_id is not None
                and self.source_component_id != self.expected_component_id
            ):
                raise Px4MissionFeedbackError(
                    "source component does not match its expected identity"
                )

        accepted = require_int(self.accepted_event_count, "accepted_event_count", minimum=0)
        duplicates = require_int(self.duplicate_event_count, "duplicate_event_count", minimum=0)
        rejected = require_int(self.rejected_event_count, "rejected_event_count", minimum=0)
        object.__setattr__(self, "accepted_event_count", accepted)
        object.__setattr__(self, "duplicate_event_count", duplicates)
        object.__setattr__(self, "rejected_event_count", rejected)
        unique_event_count = len(current_history) + len(reached)
        if accepted != unique_event_count + duplicates:
            raise Px4MissionFeedbackError(
                "accepted_event_count does not match unique and duplicate events"
            )
        if rejected == 0:
            if self.last_rejection_reason is not None:
                raise Px4MissionFeedbackError("last_rejection_reason requires a rejected event")
        else:
            object.__setattr__(
                self,
                "last_rejection_reason",
                require_text(self.last_rejection_reason, "last_rejection_reason"),
            )

        expected_phase, expected_qualified, expected_reason = _derive_snapshot_state(
            mission_item_count=count,
            current_sequence=current,
            reached_sequences=reached,
            feedback_age_s=self.feedback_age_s,
            maximum_age_s=maximum_age,
            source_identity_bound=identity_bound,
            rejected_event_count=rejected,
            last_rejection_reason=self.last_rejection_reason,
        )
        if self.phase is not expected_phase:
            raise Px4MissionFeedbackError("phase does not match the feedback state")
        if require_bool(self.qualified, "qualified") is not expected_qualified:
            raise Px4MissionFeedbackError("qualified does not match the feedback state")
        object.__setattr__(self, "qualified", expected_qualified)
        if require_text(self.reason, "reason") != expected_reason:
            raise Px4MissionFeedbackError("reason does not match the feedback state")
        object.__setattr__(self, "reason", expected_reason)

    @classmethod
    def from_mapping(cls, raw: object) -> Px4MissionFeedbackSnapshot:
        value = strict_object(
            raw,
            name="Px4MissionFeedbackSnapshot",
            required=frozenset(
                {
                    "schema_version",
                    "mission_id",
                    "mission_hash",
                    "mission_item_count",
                    "evaluated_at_s",
                    "maximum_age_s",
                    "phase",
                    "observed_current_sequences",
                    "current_sequence",
                    "current_observed_at_s",
                    "reached_sequences",
                    "last_reached_observed_at_s",
                    "last_event_observed_at_s",
                    "feedback_age_s",
                    "expected_system_id",
                    "expected_component_id",
                    "source_system_id",
                    "source_component_id",
                    "source_identity_bound",
                    "accepted_event_count",
                    "duplicate_event_count",
                    "rejected_event_count",
                    "last_rejection_reason",
                    "qualified",
                    "reason",
                }
            ),
        )
        try:
            phase = Px4MissionFeedbackPhase(require_text(value["phase"], "phase"))
        except ValueError as exc:
            raise Px4MissionFeedbackError(f"unsupported feedback phase: {exc}") from exc
        return cls(
            mission_id=require_text(value["mission_id"], "mission_id"),
            mission_hash=require_sha256(value["mission_hash"], "mission_hash"),
            mission_item_count=require_int(
                value["mission_item_count"], "mission_item_count", minimum=1
            ),
            evaluated_at_s=require_real(value["evaluated_at_s"], "evaluated_at_s", minimum=0.0),
            maximum_age_s=require_real(value["maximum_age_s"], "maximum_age_s", minimum=0.0),
            phase=phase,
            observed_current_sequences=tuple(
                require_int(item, "observed_current_sequences item", minimum=0)
                for item in require_array(
                    value["observed_current_sequences"], "observed_current_sequences"
                )
            ),
            current_sequence=_mapping_optional_int(value["current_sequence"], "current_sequence"),
            current_observed_at_s=optional_real(
                value["current_observed_at_s"], "current_observed_at_s", minimum=0.0
            ),
            reached_sequences=tuple(
                require_int(item, "reached_sequences item", minimum=0)
                for item in require_array(value["reached_sequences"], "reached_sequences")
            ),
            last_reached_observed_at_s=optional_real(
                value["last_reached_observed_at_s"],
                "last_reached_observed_at_s",
                minimum=0.0,
            ),
            last_event_observed_at_s=optional_real(
                value["last_event_observed_at_s"],
                "last_event_observed_at_s",
                minimum=0.0,
            ),
            feedback_age_s=optional_real(value["feedback_age_s"], "feedback_age_s", minimum=0.0),
            expected_system_id=_mapping_optional_int(
                value["expected_system_id"], "expected_system_id"
            ),
            expected_component_id=_mapping_optional_int(
                value["expected_component_id"], "expected_component_id"
            ),
            source_system_id=_mapping_optional_int(value["source_system_id"], "source_system_id"),
            source_component_id=_mapping_optional_int(
                value["source_component_id"], "source_component_id"
            ),
            source_identity_bound=require_bool(
                value["source_identity_bound"], "source_identity_bound"
            ),
            accepted_event_count=require_int(
                value["accepted_event_count"], "accepted_event_count", minimum=0
            ),
            duplicate_event_count=require_int(
                value["duplicate_event_count"], "duplicate_event_count", minimum=0
            ),
            rejected_event_count=require_int(
                value["rejected_event_count"], "rejected_event_count", minimum=0
            ),
            last_rejection_reason=(
                None
                if value["last_rejection_reason"] is None
                else require_text(value["last_rejection_reason"], "last_rejection_reason")
            ),
            qualified=require_bool(value["qualified"], "qualified"),
            reason=require_text(value["reason"], "reason"),
            schema_version=require_schema_version(value["schema_version"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "mission_hash": self.mission_hash,
            "mission_item_count": self.mission_item_count,
            "evaluated_at_s": self.evaluated_at_s,
            "maximum_age_s": self.maximum_age_s,
            "phase": self.phase.value,
            "observed_current_sequences": list(self.observed_current_sequences),
            "current_sequence": self.current_sequence,
            "current_observed_at_s": self.current_observed_at_s,
            "reached_sequences": list(self.reached_sequences),
            "last_reached_observed_at_s": self.last_reached_observed_at_s,
            "last_event_observed_at_s": self.last_event_observed_at_s,
            "feedback_age_s": self.feedback_age_s,
            "expected_system_id": self.expected_system_id,
            "expected_component_id": self.expected_component_id,
            "source_system_id": self.source_system_id,
            "source_component_id": self.source_component_id,
            "source_identity_bound": self.source_identity_bound,
            "accepted_event_count": self.accepted_event_count,
            "duplicate_event_count": self.duplicate_event_count,
            "rejected_event_count": self.rejected_event_count,
            "last_rejection_reason": self.last_rejection_reason,
            "qualified": self.qualified,
            "reason": self.reason,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @property
    def sha256(self) -> str:
        return self.canonical_hash


class Px4MissionFeedbackTracker:
    """Consume only PX4 Mission progress messages for one compiled Mission."""

    def __init__(
        self,
        mission: ExecutableMission,
        *,
        config: Px4MissionFeedbackConfig | None = None,
    ) -> None:
        if not isinstance(mission, ExecutableMission):
            raise Px4MissionFeedbackError("mission must be an ExecutableMission")
        if len(mission.items) > PX4_MISSION_FEEDBACK_MAX_ITEMS:
            raise Px4MissionFeedbackError("mission exceeds the feedback item budget")
        if config is not None and not isinstance(config, Px4MissionFeedbackConfig):
            raise Px4MissionFeedbackError("config must be a Px4MissionFeedbackConfig or None")
        self.mission = mission
        self.config = config or Px4MissionFeedbackConfig()
        self.reset()

    def reset(self) -> None:
        """Reset one observation session without modifying the bound Mission."""

        self._observed_current_sequences: list[int] = []
        self._current_sequence: int | None = None
        self._current_observed_at_s: float | None = None
        self._reached_sequences: list[int] = []
        self._last_reached_observed_at_s: float | None = None
        self._last_event_observed_at_s: float | None = None
        self._last_received_at_s: float | None = None
        self._source_system_id: int | None = None
        self._source_component_id: int | None = None
        self._accepted_event_count = 0
        self._duplicate_event_count = 0
        self._rejected_event_count = 0
        self._last_rejection_reason: str | None = None

    def ingest_message(self, message: object, *, observed_at_s: Real) -> bool:
        """Consume a MAVLink-shaped object or mapping.

        Unrelated message types return ``False`` without poisoning the bound
        feedback session.  A malformed relevant message is counted and latches
        the session invalid; callers can explicitly ``reset()`` before a new
        Mission observation window.
        """

        message_type = _message_type(message)
        if message_type not in _MISSION_MESSAGE_TYPES:
            return False
        try:
            timestamp = _finite(observed_at_s, "observed_at_s")
            if timestamp < 0.0:
                raise Px4MissionFeedbackError("observed_at_s must be non-negative")
            if self._last_received_at_s is not None and timestamp < self._last_received_at_s:
                raise Px4MissionFeedbackError("feedback_receive_timestamp_regressed")
            sequence = _message_sequence(message, len(self.mission.items))
            source_system, source_component = _message_source_identity(message)
            self._accept_source_identity(source_system, source_component)
            self._accept_sequence(message_type, sequence, timestamp)
        except (Px4MissionFeedbackError, TypeError, ValueError, OverflowError) as exc:
            return self._reject(_normalise_rejection_reason(exc))
        self._last_received_at_s = timestamp
        self._accepted_event_count += 1
        return True

    def snapshot(self, *, now_s: Real) -> Px4MissionFeedbackSnapshot:
        evaluated = _finite(now_s, "now_s")
        if evaluated < 0.0:
            raise Px4MissionFeedbackError("now_s must be non-negative")
        if (
            self._last_event_observed_at_s is not None
            and evaluated < self._last_event_observed_at_s
        ):
            raise Px4MissionFeedbackError("now_s cannot precede the last feedback event")
        feedback_age = (
            None
            if self._last_event_observed_at_s is None
            else evaluated - self._last_event_observed_at_s
        )
        source_bound = self._source_system_id is not None and self._source_component_id is not None
        phase, qualified, reason = _derive_snapshot_state(
            mission_item_count=len(self.mission.items),
            current_sequence=self._current_sequence,
            reached_sequences=tuple(self._reached_sequences),
            feedback_age_s=feedback_age,
            maximum_age_s=self.config.maximum_age_s,
            source_identity_bound=source_bound,
            rejected_event_count=self._rejected_event_count,
            last_rejection_reason=self._last_rejection_reason,
        )
        return Px4MissionFeedbackSnapshot(
            mission_id=self.mission.mission_id,
            mission_hash=self.mission.canonical_hash,
            mission_item_count=len(self.mission.items),
            evaluated_at_s=evaluated,
            maximum_age_s=self.config.maximum_age_s,
            phase=phase,
            observed_current_sequences=tuple(self._observed_current_sequences),
            current_sequence=self._current_sequence,
            current_observed_at_s=self._current_observed_at_s,
            reached_sequences=tuple(self._reached_sequences),
            last_reached_observed_at_s=self._last_reached_observed_at_s,
            last_event_observed_at_s=self._last_event_observed_at_s,
            feedback_age_s=feedback_age,
            expected_system_id=self.config.expected_system_id,
            expected_component_id=self.config.expected_component_id,
            source_system_id=self._source_system_id,
            source_component_id=self._source_component_id,
            source_identity_bound=source_bound,
            accepted_event_count=self._accepted_event_count,
            duplicate_event_count=self._duplicate_event_count,
            rejected_event_count=self._rejected_event_count,
            last_rejection_reason=self._last_rejection_reason,
            qualified=qualified,
            reason=reason,
        )

    def _accept_source_identity(
        self,
        source_system_id: int | None,
        source_component_id: int | None,
    ) -> None:
        if (source_system_id is None) != (source_component_id is None):
            raise Px4MissionFeedbackError("feedback_source_identity_incomplete")
        expected_system = self.config.expected_system_id
        expected_component = self.config.expected_component_id
        if expected_system is not None and source_system_id != expected_system:
            raise Px4MissionFeedbackError("feedback_source_system_mismatch")
        if expected_component is not None and source_component_id != expected_component:
            raise Px4MissionFeedbackError("feedback_source_component_mismatch")
        if source_system_id is None:
            return
        if self._source_system_id is None:
            self._source_system_id = source_system_id
            self._source_component_id = source_component_id
            return
        if (
            source_system_id != self._source_system_id
            or source_component_id != self._source_component_id
        ):
            raise Px4MissionFeedbackError("feedback_source_identity_changed")

    def _accept_sequence(self, message_type: str, sequence: int, timestamp: float) -> None:
        if message_type == "MISSION_CURRENT":
            reached_cursor = self._reached_sequences[-1] if self._reached_sequences else None
            if self._current_sequence is not None and sequence < self._current_sequence:
                raise Px4MissionFeedbackError("mission_current_sequence_regressed")
            if reached_cursor is not None and sequence < reached_cursor:
                raise Px4MissionFeedbackError("mission_current_precedes_reached_sequence")
            if sequence == self._current_sequence:
                self._duplicate_event_count += 1
            else:
                self._observed_current_sequences.append(sequence)
                self._current_sequence = sequence
            self._current_observed_at_s = timestamp
            self._last_event_observed_at_s = timestamp
            return

        reached_cursor = self._reached_sequences[-1] if self._reached_sequences else None
        if sequence in self._reached_sequences:
            self._duplicate_event_count += 1
            if sequence == reached_cursor:
                self._last_reached_observed_at_s = timestamp
                self._last_event_observed_at_s = timestamp
            return
        if reached_cursor is not None and sequence < reached_cursor:
            raise Px4MissionFeedbackError("mission_item_reached_sequence_regressed")
        if self._current_sequence is not None and sequence > self._current_sequence:
            raise Px4MissionFeedbackError("mission_item_reached_ahead_of_current_sequence")
        self._reached_sequences.append(sequence)
        self._last_reached_observed_at_s = timestamp
        self._last_event_observed_at_s = timestamp

    def _reject(self, reason: str) -> bool:
        self._rejected_event_count += 1
        self._last_rejection_reason = reason
        return False


def _derive_snapshot_state(
    *,
    mission_item_count: int,
    current_sequence: int | None,
    reached_sequences: tuple[int, ...],
    feedback_age_s: float | None,
    maximum_age_s: float,
    source_identity_bound: bool,
    rejected_event_count: int,
    last_rejection_reason: str | None,
) -> tuple[Px4MissionFeedbackPhase, bool, str]:
    if rejected_event_count:
        return (
            Px4MissionFeedbackPhase.INVALID,
            False,
            last_rejection_reason or "feedback_rejected",
        )
    if feedback_age_s is None:
        return Px4MissionFeedbackPhase.AWAITING, False, "mission_feedback_unavailable"
    if feedback_age_s > maximum_age_s:
        return Px4MissionFeedbackPhase.STALE, False, "mission_feedback_stale"
    terminal_reached = mission_item_count - 1 in reached_sequences
    phase = (
        Px4MissionFeedbackPhase.TERMINAL_REACHED
        if terminal_reached
        else Px4MissionFeedbackPhase.ACTIVE
    )
    if current_sequence is None:
        return phase, False, "mission_current_unavailable"
    if not source_identity_bound:
        return phase, False, "mission_feedback_source_unbound"
    if terminal_reached:
        return phase, True, "terminal_mission_item_reached"
    return phase, True, "mission_feedback_qualified"


def _sequence_history(value: object, name: str, *, item_count: int) -> tuple[int, ...]:
    try:
        result = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise Px4MissionFeedbackError(f"{name} must be an iterable of sequences") from exc
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item < 0 or item >= item_count
        for item in result
    ):
        raise Px4MissionFeedbackError(f"{name} contains an invalid sequence")
    if result != tuple(sorted(set(result))):
        raise Px4MissionFeedbackError(f"{name} must be strictly increasing and unique")
    return result


def _optional_sequence(value: object, name: str, item_count: int) -> int | None:
    if value is None:
        return None
    return require_int(value, name, minimum=0, maximum=item_count - 1)


def _mapping_optional_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    return require_int(value, name, minimum=0)


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
        raise Px4MissionFeedbackError(f"{name} must be finite numeric")
    return float(value)


def _mavlink_source_id(value: object, name: str) -> int:
    return require_int(value, name, minimum=1, maximum=255)


def _message_type(message: object) -> str:
    raw: object | None = None
    if isinstance(message, Mapping):
        for name in ("message_type", "mavpackettype", "type"):
            if name in message:
                raw = message[name]
                break
    else:
        get_type = getattr(message, "get_type", None)
        if callable(get_type):
            raw = get_type()
    return raw.strip().upper() if isinstance(raw, str) else ""


def _message_value(message: object, name: str) -> object | None:
    if isinstance(message, Mapping):
        return message.get(name)
    return getattr(message, name, None)


def _message_sequence(message: object, item_count: int) -> int:
    return require_int(
        _message_value(message, "seq"),
        "mission feedback seq",
        minimum=0,
        maximum=item_count - 1,
    )


def _message_source_identity(message: object) -> tuple[int | None, int | None]:
    system_raw: object | None
    component_raw: object | None
    if isinstance(message, Mapping):
        system_raw = message.get("source_system_id", message.get("srcSystem"))
        component_raw = message.get("source_component_id", message.get("srcComponent"))
    else:
        get_system = getattr(message, "get_srcSystem", None)
        get_component = getattr(message, "get_srcComponent", None)
        system_raw = get_system() if callable(get_system) else getattr(message, "srcSystem", None)
        component_raw = (
            get_component() if callable(get_component) else getattr(message, "srcComponent", None)
        )
    system = None if system_raw is None else _mavlink_source_id(system_raw, "source_system_id")
    component = (
        None if component_raw is None else _mavlink_source_id(component_raw, "source_component_id")
    )
    return system, component


def _normalise_rejection_reason(exc: Exception) -> str:
    reason = str(exc).strip().lower().replace(" ", "_")
    return reason or type(exc).__name__.lower()


__all__ = [
    "PX4_MISSION_FEEDBACK_CANONICAL_MAX_AGE_S",
    "PX4_MISSION_FEEDBACK_MAX_ITEMS",
    "PX4_MISSION_FEEDBACK_SCHEMA_VERSION",
    "Px4MissionFeedbackConfig",
    "Px4MissionFeedbackError",
    "Px4MissionFeedbackPhase",
    "Px4MissionFeedbackSnapshot",
    "Px4MissionFeedbackTracker",
]
