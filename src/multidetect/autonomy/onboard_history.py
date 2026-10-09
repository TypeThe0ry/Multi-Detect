"""Versioned, bounded observation history; no action labels or actuator access.

The clock epoch is an explicit caller/session identity, not a claim about a
Gazebo sensor clock. Scope changes and gaps require an audited explicit reset.
Only immutable truth-free OnboardModelInput values enter the window.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import TYPE_CHECKING
from uuid import UUID

from .contracts import ContextSnapshot
from .serialization import (
    canonical_sha256,
    require_bool,
    require_int,
    require_real,
    require_sha256,
    require_text,
)

if TYPE_CHECKING:
    from .onboard_agent import OnboardModelInput

ONBOARD_HISTORY_CONTRACT = "multidetect.onboard-history-input-v1"
ONBOARD_HISTORY_LENGTH = 8
ONBOARD_HISTORY_FEATURE_WIDTH = 40
ONBOARD_HISTORY_OUTPUT_WIDTH = 20
ONBOARD_HISTORY_MAX_GAP_S = 5.0


class OnboardHistoryError(ValueError):
    """History identity, time or immutable input validation failed closed."""


@dataclass(frozen=True, slots=True)
class OnboardInferenceScope:
    session_id: str
    prompt_sha256: str
    aircraft_id: str
    selected_task_id: str
    airframe_profile_id: str
    airframe_profile_sha256: str
    vehicle_class: str
    home_sha256: str
    clock_epoch: str

    def __post_init__(self) -> None:
        session = require_text(self.session_id, "session_id")
        try:
            normalized = str(UUID(session))
        except ValueError as exc:
            raise OnboardHistoryError("session_id must be a UUID") from exc
        if normalized != session:
            raise OnboardHistoryError("session_id must be a canonical UUID")
        object.__setattr__(self, "session_id", normalized)
        for name in ("prompt_sha256", "airframe_profile_sha256", "home_sha256"):
            object.__setattr__(self, name, require_sha256(getattr(self, name), name))
        for name in ("aircraft_id", "selected_task_id", "airframe_profile_id", "clock_epoch"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        if self.vehicle_class not in {"multirotor", "fixed-wing", "vtol"}:
            raise OnboardHistoryError("vehicle_class must be canonical")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "contract": "multidetect.onboard-inference-scope-v1",
            **{field.name: getattr(self, field.name) for field in fields(self)},
            "clock_epoch_semantics": "explicit_runtime_session_not_sensor_clock_claim",
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    def validate_context(self, context: ContextSnapshot) -> None:
        if not isinstance(context, ContextSnapshot):
            raise TypeError("context must be a ContextSnapshot")
        if (
            context.airframe_profile_id != self.airframe_profile_id
            or context.airframe_profile_hash != self.airframe_profile_sha256
            or context.home_position.canonical_hash != self.home_sha256
        ):
            raise OnboardHistoryError("input context does not match inference scope")


@dataclass(frozen=True, slots=True)
class OnboardHistoryEntry:
    event_id: str
    input_sha256: str
    observed_at_s: float
    captured_at_s: float
    features: tuple[float, ...]
    dt_s: float
    reset: bool

    def __post_init__(self) -> None:
        require_text(self.event_id, "event_id")
        require_sha256(self.input_sha256, "input_sha256")
        require_real(self.observed_at_s, "observed_at_s", minimum=0)
        require_real(self.captured_at_s, "captured_at_s", minimum=self.observed_at_s)
        require_real(self.dt_s, "dt_s", minimum=0)
        require_bool(self.reset, "reset")
        if (
            not isinstance(self.features, tuple)
            or len(self.features) != ONBOARD_HISTORY_FEATURE_WIDTH
        ):
            raise OnboardHistoryError("history features must be an immutable 40-value tuple")
        for value in self.features:
            require_real(value, "feature")

    def to_mapping(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "input_sha256": self.input_sha256,
            "observed_at_s": self.observed_at_s,
            "captured_at_s": self.captured_at_s,
            "features": list(self.features),
            "dt_s": self.dt_s,
            "reset": self.reset,
        }


@dataclass(frozen=True, slots=True)
class OnboardHistoryWindow:
    scope: OnboardInferenceScope
    entries: tuple[OnboardHistoryEntry, ...]
    base_revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.scope, OnboardInferenceScope):
            raise TypeError("scope must be an OnboardInferenceScope")
        require_int(self.base_revision, "base_revision", minimum=0)
        if (
            not isinstance(self.entries, tuple)
            or not 1 <= len(self.entries) <= ONBOARD_HISTORY_LENGTH
            or any(not isinstance(entry, OnboardHistoryEntry) for entry in self.entries)
        ):
            raise OnboardHistoryError("history window requires 1..8 immutable entries")

    @property
    def features(self) -> tuple[tuple[float, ...], ...]:
        padding = ONBOARD_HISTORY_LENGTH - len(self.entries)
        return ((0.0,) * ONBOARD_HISTORY_FEATURE_WIDTH,) * padding + tuple(
            entry.features for entry in self.entries
        )

    @property
    def valid_mask(self) -> tuple[float, ...]:
        return (0.0,) * (ONBOARD_HISTORY_LENGTH - len(self.entries)) + (1.0,) * len(self.entries)

    @property
    def dt_s(self) -> tuple[float, ...]:
        return (0.0,) * (ONBOARD_HISTORY_LENGTH - len(self.entries)) + tuple(
            entry.dt_s for entry in self.entries
        )

    @property
    def reset_mask(self) -> tuple[float, ...]:
        return (0.0,) * (ONBOARD_HISTORY_LENGTH - len(self.entries)) + tuple(
            float(entry.reset) for entry in self.entries
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "contract": ONBOARD_HISTORY_CONTRACT,
            "scope": self.scope.to_mapping(),
            "entries": [entry.to_mapping() for entry in self.entries],
            "valid_mask": list(self.valid_mask),
            "dt_s": list(self.dt_s),
            "reset_mask": list(self.reset_mask),
            "alignment": "right_current_last",
            "truth_free": True,
            "training_ready": False,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


class OnboardHistoryBuffer:
    """Prepare/commit windows so a failed inference cannot consume a frame.

    The caller serializes prepare/infer/commit. A revision guard rejects stale
    prepared transactions. Memory remains bounded to eight immutable entries.
    """

    __slots__ = ("_max_gap_s", "_scope", "_entries", "_revision", "_last_reset", "_pending")

    def __init__(self, *, max_gap_s: float = ONBOARD_HISTORY_MAX_GAP_S) -> None:
        self._max_gap_s = require_real(max_gap_s, "max_gap_s", minimum=0.0)
        if self._max_gap_s == 0.0:
            raise OnboardHistoryError("max_gap_s must be positive")
        self._scope: OnboardInferenceScope | None = None
        self._entries: tuple[OnboardHistoryEntry, ...] = ()
        self._revision = 0
        self._last_reset: dict[str, object] | None = None
        self._pending: OnboardHistoryWindow | None = None

    @property
    def max_gap_s(self) -> float:
        return self._max_gap_s

    @property
    def entries(self) -> tuple[OnboardHistoryEntry, ...]:
        return self._entries

    @property
    def scope(self) -> OnboardInferenceScope | None:
        return self._scope

    @property
    def last_reset_receipt(self) -> dict[str, object] | None:
        return None if self._last_reset is None else dict(self._last_reset)

    def reset(self, *, reason: str, scope: OnboardInferenceScope | None = None) -> None:
        reason = require_text(reason, "reset reason")
        if scope is not None and not isinstance(scope, OnboardInferenceScope):
            raise TypeError("scope must be an OnboardInferenceScope or None")
        self._last_reset = {
            "contract": "multidetect.onboard-history-reset-v1",
            "reason": reason,
            "previous_scope_sha256": None if self._scope is None else self._scope.canonical_hash,
            "next_scope_sha256": None if scope is None else scope.canonical_hash,
            "discarded_entries": len(self._entries),
            "revision": self._revision + 1,
            "truth_free": True,
            "training_ready": False,
        }
        self._scope = scope
        self._entries = ()
        self._pending = None
        self._revision += 1

    def prepare(
        self, model_input: OnboardModelInput, scope: OnboardInferenceScope
    ) -> OnboardHistoryWindow:
        from .onboard_agent import OnboardModelInput

        if not isinstance(model_input, OnboardModelInput):
            raise TypeError("model_input must be an OnboardModelInput")
        if not isinstance(scope, OnboardInferenceScope):
            raise TypeError("scope must be an OnboardInferenceScope")
        if self._scope is not None and scope != self._scope:
            raise OnboardHistoryError("inference scope changed; explicit reset required")
        scope.validate_context(model_input.context)
        timestamp = require_real(model_input.event.observed_at_s, "observed_at_s", minimum=0)
        captured_at = require_real(model_input.context.captured_at_s, "captured_at_s", minimum=0)
        if timestamp > captured_at:
            raise OnboardHistoryError("event is newer than its context")
        features = tuple(require_real(value, "feature") for value in model_input.feature_vector())
        if len(features) != ONBOARD_HISTORY_FEATURE_WIDTH:
            raise OnboardHistoryError("history requires the current 40-feature order")
        input_hash = canonical_sha256(model_input.to_mapping())
        event_id = model_input.event.event_id
        for index, previous in enumerate(self._entries):
            if previous.event_id == event_id:
                if previous.input_sha256 != input_hash:
                    raise OnboardHistoryError("duplicate event ID has conflicting input")
                if index != len(self._entries) - 1:
                    raise OnboardHistoryError("stale event retry cannot rewind history")
                self._pending = OnboardHistoryWindow(scope, self._entries, self._revision)
                return self._pending
        dt_s = 0.0
        if self._entries:
            previous = self._entries[-1]
            dt_s = timestamp - previous.observed_at_s
            if dt_s <= 0 or captured_at < previous.captured_at_s:
                raise OnboardHistoryError("history timestamp regressed or repeated")
            if timestamp < previous.captured_at_s:
                raise OnboardHistoryError("event predates the previous context")
            if dt_s > self.max_gap_s:
                raise OnboardHistoryError("history expired across a gap; explicit reset required")
        entry = OnboardHistoryEntry(
            event_id, input_hash, timestamp, captured_at, features, dt_s, not self._entries
        )
        entries = (*self._entries, entry)[-ONBOARD_HISTORY_LENGTH:]
        self._pending = OnboardHistoryWindow(scope, entries, self._revision)
        return self._pending

    def commit(self, window: OnboardHistoryWindow) -> None:
        if not isinstance(window, OnboardHistoryWindow):
            raise TypeError("window must be an OnboardHistoryWindow")
        if window.base_revision != self._revision or window is not self._pending:
            raise OnboardHistoryError("stale history transaction")
        if self._scope is not None and self._scope != window.scope:
            raise OnboardHistoryError("history transaction scope mismatch")
        self._scope = window.scope
        if window.entries != self._entries:
            self._entries = window.entries
            self._revision += 1
        self._pending = None
