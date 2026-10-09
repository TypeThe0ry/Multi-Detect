"""Receive-only sortie outcomes on the existing Mission feedback tracker.

Terminal Mission progress alone is not touchdown. Completion also requires
fresh, source-bound landed/disarmed telemetry observed in this session.
This monitor grants no authority to start another mission.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..clock import Clock
from ..px4_mission_feedback import (
    Px4MissionFeedbackConfig,
    Px4MissionFeedbackPhase,
    Px4MissionFeedbackTracker,
)
from .contracts import ExecutableMission
from .serialization import require_bool, require_int, require_real


@dataclass(frozen=True, slots=True)
class MissionOutcome:
    mission_hash: str
    status: str
    reason: str
    elapsed_s: float


class MissionOutcomeMonitor:
    """One non-reusable observation session with latched terminal outcomes."""

    # PX4 can deliver a short burst of MISSION_ITEM_REACHED messages before
    # the corresponding MISSION_CURRENT update.  Keep this reconciliation
    # bounded: a missing current update still fails closed at the canonical
    # feedback freshness deadline, and an unbounded stream cannot consume
    # memory in an adapter process.
    _MAX_PENDING_REACHED_EVENTS = 8

    def __init__(
        self,
        mission: ExecutableMission,
        clock: Clock,
        *,
        timeout_s: float,
        expected_system_id: int,
        expected_component_id: int,
    ) -> None:
        self._timeout = require_real(timeout_s, "timeout_s", minimum=0.0)
        if self._timeout <= 0:
            raise ValueError("timeout_s must be positive")
        self._tracker = Px4MissionFeedbackTracker(
            mission,
            config=Px4MissionFeedbackConfig(
                expected_system_id=expected_system_id,
                expected_component_id=expected_component_id,
            ),
        )
        if expected_system_id is None or expected_component_id is None:
            raise ValueError("both expected source IDs are required")
        self._clock = clock
        self._start = require_real(clock.monotonic(), "clock", minimum=0.0)
        self._last_now = self._start
        self._lifecycle: tuple[float, bool, bool] | None = None
        self._terminal: MissionOutcome | None = None
        self._armed_sample: tuple[float, bool] | None = None
        self._landed_sample: tuple[float, bool] | None = None
        self._pending_reached: dict[int, tuple[float, dict[str, object]]] = {}
        self.reconciliations: list[dict[str, object]] = []

    def _now(self) -> float:
        now = require_real(self._clock.monotonic(), "clock", minimum=0.0)
        if now < self._last_now:
            raise ValueError("observation clock regressed")
        self._last_now = now
        return now

    def _finish(self, status: str, reason: str, now: float) -> MissionOutcome:
        self._terminal = MissionOutcome(
            self._tracker.mission.canonical_hash,
            status,
            reason,
            now - self._start,
        )
        return self._terminal

    def ingest_message(self, message: object) -> bool:
        """Stamp received Mission feedback with the same injected clock."""
        if self.poll().status in {"completed", "failed", "timeout"}:
            return False
        now = self._now()
        kind = (
            message.get("message_type", message.get("mavpackettype"))
            if isinstance(message, Mapping)
            else message.get_type()
            if callable(getattr(message, "get_type", None))
            else None
        )
        if kind in {"HEARTBEAT", "EXTENDED_SYS_STATE"}:
            return self._ingest_lifecycle_message(message, kind, now)
        before = self._tracker.snapshot(now_s=now)
        if kind == "MISSION_ITEM_REACHED" and before.current_sequence is not None:
            try:
                if isinstance(message, Mapping):
                    seq = message.get("seq")
                    system = message.get("source_system_id", message.get("srcSystem"))
                    component = message.get("source_component_id", message.get("srcComponent"))
                else:
                    seq = message.seq
                    system = message.get_srcSystem()
                    component = message.get_srcComponent()
                seq = require_int(
                    seq, "seq", minimum=0, maximum=len(self._tracker.mission.items) - 1
                )
                system = require_int(system, "source_system_id", minimum=1, maximum=255)
                component = require_int(component, "source_component_id", minimum=1, maximum=255)
            except (ValueError, TypeError, AttributeError):
                return self._tracker.ingest_message(message, observed_at_s=now)
            if (
                seq > before.current_sequence
                and system == self._tracker.config.expected_system_id
                and component == self._tracker.config.expected_component_id
            ):
                if seq not in self._pending_reached:
                    if len(self._pending_reached) >= self._MAX_PENDING_REACHED_EVENTS:
                        return self._tracker.ingest_message(message, observed_at_s=now)
                    self._pending_reached[seq] = (
                        now,
                        {
                            "message_type": kind,
                            "seq": seq,
                            "source_system_id": system,
                            "source_component_id": component,
                        },
                    )
                # Repeated messages cannot extend the original deadline.
                return True
        accepted = self._tracker.ingest_message(message, observed_at_s=now)
        if accepted and kind == "MISSION_CURRENT" and self._pending_reached:
            current = self._tracker.snapshot(now_s=now).current_sequence
            if current is not None:
                confirmed = [
                    sequence
                    for sequence in sorted(self._pending_reached)
                    if sequence <= current
                ]
                for sequence in confirmed:
                    received, pending = self._pending_reached.pop(sequence)
                    # The strict tracker sees confirmation/processing time, not
                    # a backdated receive time. The raw receive time stays in
                    # audit, and each pending sequence is reconciled in order.
                    pending_accepted = self._tracker.ingest_message(
                        pending, observed_at_s=now
                    )
                    accepted = accepted and pending_accepted
                    self.reconciliations.append(
                        {
                            "sequence": sequence,
                            "received_at_s": received,
                            "confirmed_at_s": now,
                            "delay_s": now - received,
                            "accepted": pending_accepted,
                        }
                    )
                    if not pending_accepted:
                        break
        after = self._tracker.snapshot(now_s=now)
        if (
            accepted
            and after.phase is Px4MissionFeedbackPhase.TERMINAL_REACHED
            and before.phase is not Px4MissionFeedbackPhase.TERMINAL_REACHED
        ):
            self._lifecycle = None
            self._armed_sample = None
            self._landed_sample = None
        return accepted

    def _ingest_lifecycle_message(self, message: object, kind: str, now: float) -> bool:
        def field(name: str) -> object:
            return (
                message.get(name) if isinstance(message, Mapping) else getattr(message, name, None)
            )

        try:
            if isinstance(message, Mapping):
                system = message.get("source_system_id", message.get("srcSystem"))
                component = message.get("source_component_id", message.get("srcComponent"))
            else:
                system = message.get_srcSystem()
                component = message.get_srcComponent()
            system = require_int(system, "source_system_id", minimum=1, maximum=255)
            component = require_int(component, "source_component_id", minimum=1, maximum=255)
            if (system, component) != (
                self._tracker.config.expected_system_id,
                self._tracker.config.expected_component_id,
            ):
                raise ValueError("lifecycle_source_mismatch")
            if kind == "HEARTBEAT":
                base_mode = require_int(field("base_mode"), "base_mode", minimum=0, maximum=255)
                self._armed_sample = (now, bool(base_mode & 128))
            else:
                state = require_int(field("landed_state"), "landed_state", minimum=0, maximum=4)
                self._landed_sample = (now, state == 1)
        except (ValueError, TypeError, AttributeError) as exc:
            self._finish("failed", str(exc), now)
            return False
        self._lifecycle = None
        if self._armed_sample is not None and self._landed_sample is not None:
            armed_at, armed = self._armed_sample
            landed_at, landed = self._landed_sample
            # Preserve the older receive timestamp: never freshen an old half.
            self._lifecycle = (min(armed_at, landed_at), landed, armed)
        return True

    def observe_lifecycle(
        self,
        *,
        landed: bool,
        armed: bool,
        source_system_id: int,
        source_component_id: int,
    ) -> None:
        """Adapter supplies a coherent observed lifecycle sample, not defaults."""
        require_bool(landed, "landed")
        require_bool(armed, "armed")
        if self.poll().status in {"completed", "failed", "timeout"}:
            return
        now = self._now()
        if (
            (source_system_id, source_component_id)
            != (
                self._tracker.config.expected_system_id,
                self._tracker.config.expected_component_id,
            )
            or isinstance(source_system_id, bool)
            or isinstance(source_component_id, bool)
        ):
            self._finish("failed", "lifecycle_source_mismatch", now)
            return
        self._lifecycle = (now, landed, armed)

    def poll(self) -> MissionOutcome:
        if self._terminal is not None:
            return self._terminal
        now = self._now()
        feedback = self._tracker.snapshot(now_s=now)
        if feedback.phase is Px4MissionFeedbackPhase.INVALID:
            return self._finish("failed", feedback.reason, now)
        if now - self._start >= self._timeout:
            return self._finish("timeout", "mission_deadline_exceeded", now)
        if self._pending_reached:
            # Use the shared freshness budget, not a single-run latency guess.
            # Confirmation must arrive before the original event becomes stale.
            if isinstance(self._pending_reached, tuple):
                # Keep compatibility with older in-process test fixtures that
                # seeded the former single-pending tuple directly.
                pending_values = (self._pending_reached,)
            else:
                pending_values = tuple(self._pending_reached.values())
            oldest_received = min(received for received, _pending in pending_values)
            if now - oldest_received >= self._tracker.config.maximum_age_s:
                return self._finish("failed", "reached_confirmation_deadline_exceeded", now)
            return MissionOutcome(
                feedback.mission_hash,
                "waiting",
                "awaiting_current_confirmation",
                now - self._start,
            )
        if feedback.qualified and feedback.phase is Px4MissionFeedbackPhase.TERMINAL_REACHED:
            if self._lifecycle is not None:
                stamp, landed, armed = self._lifecycle
                # A pre-terminal ground sample must not complete a later flight.
                if (
                    stamp >= feedback.last_reached_observed_at_s
                    and now - stamp <= feedback.maximum_age_s
                    and landed
                    and not armed
                ):
                    return self._finish("completed", "terminal_reached_landed_disarmed", now)
            reason = "awaiting_fresh_landed_disarmed"
        else:
            reason = feedback.reason
        return MissionOutcome(
            feedback.mission_hash,
            "waiting",
            reason,
            now - self._start,
        )
