from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import hypot, isfinite

from .domain import BoundingBox
from .operator_bridge import OperatorBridgeResult
from .operator_link import (
    SelectionAction,
    TargetSelectionCommand,
    TrackingState,
    TrackStatusMessage,
)
from .unified_tracking import (
    TargetObservation,
    UnifiedTargetPool,
    UnifiedTrackSnapshot,
    UnifiedTrackState,
)


@dataclass(frozen=True, slots=True)
class SelectionTargetPoolConfig:
    minimum_iou: float = 0.05
    maximum_center_distance: float = 0.18
    manual_observation_confidence: float = 0.70
    mapping_history_size: int = 256
    ambiguity_iou_margin: float = 0.08
    ambiguity_center_distance_margin: float = 0.03
    minimum_lock_confidence: float = 0.35
    minimum_lock_tracking_quality: float = 0.45

    def __post_init__(self) -> None:
        for name, value in (
            ("minimum_iou", self.minimum_iou),
            ("maximum_center_distance", self.maximum_center_distance),
            ("manual_observation_confidence", self.manual_observation_confidence),
            ("ambiguity_iou_margin", self.ambiguity_iou_margin),
            ("ambiguity_center_distance_margin", self.ambiguity_center_distance_margin),
            ("minimum_lock_confidence", self.minimum_lock_confidence),
            ("minimum_lock_tracking_quality", self.minimum_lock_tracking_quality),
        ):
            if not isfinite(value) or not 0.0 < value <= 1.0:
                raise ValueError(f"{name} must be in (0, 1]")
        if (
            isinstance(self.mapping_history_size, bool)
            or not isinstance(self.mapping_history_size, int)
            or self.mapping_history_size < 16
        ):
            raise ValueError("mapping_history_size must be an integer >= 16")


@dataclass(frozen=True, slots=True)
class SelectionTargetPoolSync:
    active_selection_command_id: str | None
    active_track_id: str | None
    tracked_track_ids: tuple[str, ...] = ()
    bound_track_id: str | None = None
    unlocked_track_id: str | None = None
    pending_manual_observation: bool = False
    background_locked_track_ids: tuple[str, ...] = ()
    primary_switch_latency_ms: float | None = None
    reason: str | None = None
    metadata_only: bool = True
    flight_control_enabled: bool = False

    def __post_init__(self) -> None:
        if not self.metadata_only or self.flight_control_enabled:
            raise ValueError("selection target-pool synchronization must remain metadata-only")


@dataclass(frozen=True, slots=True)
class _PendingManualObservation:
    command_id: str
    status_target_id: str
    bbox: BoundingBox
    confidence: float
    action: SelectionAction


class UnifiedSelectionTargetPool:
    """Map operator/manual selections into the shared target bank without control authority."""

    def __init__(
        self,
        target_pool: UnifiedTargetPool,
        config: SelectionTargetPoolConfig | None = None,
    ) -> None:
        self.target_pool = target_pool
        self.config = config or SelectionTargetPoolConfig()
        self._active_command_id: str | None = None
        self._active_track_id: str | None = None
        self._active_session_id: str | None = None
        self._tracked_track_ids: set[str] = set()
        self._active_action: SelectionAction | None = None
        # Multiple manual rectangles can be drawn before the next detector/pool
        # update. Preserve each command independently instead of allowing the
        # newest rectangle to overwrite the preceding one.
        self._pending: dict[str, _PendingManualObservation] = {}
        self._command_to_track: dict[str, str] = {}
        self._mapping_order: deque[str] = deque()

    @property
    def active_track_id(self) -> str | None:
        return self._active_track_id

    @property
    def active_command_id(self) -> str | None:
        return self._active_command_id

    @property
    def tracked_track_ids(self) -> tuple[str, ...]:
        """All tracks explicitly selected by the current operator session."""

        return tuple(sorted(self._tracked_track_ids))

    @property
    def exclusive_lock_track_id(self) -> str | None:
        """Return the sole primary LCK target that receives high-rate tracking."""

        track_id = self._active_track_id
        if track_id is None or self._tracked_track_ids != {track_id}:
            return None
        snapshot = self._snapshot_by_id(track_id)
        if snapshot is None or not snapshot.locked or not snapshot.primary:
            return None
        return track_id

    @property
    def exclusive_high_rate(self) -> bool:
        return self.exclusive_lock_track_id is not None

    @property
    def visual_confirmation_track_ids(self) -> tuple[str, ...]:
        """Explicit TRK targets whose validated local visual motion may bridge gaps.

        The common-object detector intentionally runs on a cadence.  Leaving a
        selected, recognized target out of this set made it alternate between
        ``OCCLUDED`` and ``RECOVERED`` on the detector's skipped frames even
        while the short-term tracker had a fresh, validated motion hint.  Keep
        the bridge scoped to an operator-selected identity: ordinary DET
        candidates still rely exclusively on detector observations, and a
        genuinely LOST identity still needs detector/ReID evidence to return.
        """

        snapshots = {track.track_id: track for track in self.target_pool.snapshots()}
        return tuple(
            sorted(
                track_id
                for track_id in self._tracked_track_ids
                if track_id in snapshots
                and snapshots[track_id].state is not UnifiedTrackState.LOST
            )
        )

    def observations_for_next_pool_update(self) -> tuple[TargetObservation, ...]:
        if not self._pending:
            return ()
        return tuple(
            TargetObservation(
                label="manual",
                confidence=pending.confidence,
                bbox=pending.bbox,
                appearance_reliable=False,
                source="operator_manual_selection",
            )
            for pending in self._pending.values()
        )

    def after_pool_update(self, *, now_s: float) -> SelectionTargetPoolSync:
        self._validate_now(now_s)
        snapshots = self.target_pool.snapshots()
        existing_track_ids = {track.track_id for track in snapshots}
        self._tracked_track_ids.intersection_update(existing_track_ids)
        if self._active_track_id not in existing_track_ids:
            self._active_track_id = None
        pending_items = tuple(self._pending.values())
        if not pending_items:
            return self._sync()
        # A manual observation is only the fallback identity. If the detector has
        # already produced a reliable object at the same location, bind each
        # operator interaction to that object and leave its temporary manual track
        # unselected. This prevents one drawn rectangle from becoming both
        # ``TRK manual`` and ``TRK <class>`` in the target-pool metadata.
        recognized = tuple(track for track in snapshots if track.label != "manual")
        still_pending: dict[str, _PendingManualObservation] = {}
        latest_result = self._sync()
        last_reason: str | None = None
        for pending in pending_items:
            candidate = self._candidate(recognized, pending.bbox, preferred_label=None)
            if candidate is None:
                candidate = self._candidate(
                    snapshots,
                    pending.bbox,
                    preferred_label="manual",
                )
            if candidate is None:
                still_pending[pending.command_id] = pending
                last_reason = "manual observation was not associated with a target-pool track"
                continue
            result = self._apply_interaction(
                command_id=pending.command_id,
                candidate=candidate,
                now_s=now_s,
                action=pending.action,
            )
            if result.active_track_id == candidate.track_id:
                latest_result = result
                continue
            still_pending[pending.command_id] = pending
            last_reason = result.reason or (
                "manual observation is still waiting for a stable target-pool track"
            )
        self._pending = still_pending
        if still_pending:
            return self._sync(
                bound_track_id=latest_result.bound_track_id,
                unlocked_track_id=latest_result.unlocked_track_id,
                pending_manual_observation=True,
                background_locked_track_ids=latest_result.background_locked_track_ids,
                primary_switch_latency_ms=latest_result.primary_switch_latency_ms,
                reason=last_reason,
            )
        return self._sync(
            bound_track_id=latest_result.bound_track_id,
            unlocked_track_id=latest_result.unlocked_track_id,
            background_locked_track_ids=latest_result.background_locked_track_ids,
            primary_switch_latency_ms=latest_result.primary_switch_latency_ms,
            reason=latest_result.reason,
        )

    def consume_bridge_result(
        self,
        result: OperatorBridgeResult,
        *,
        now_s: float,
    ) -> SelectionTargetPoolSync:
        self._validate_now(now_s)
        latest_sync = self._sync()
        for command, _peer in result.accepted_selection_commands:
            if command.action is SelectionAction.CANCEL:
                latest_sync = self._cancel(now_s=now_s)
                continue
            if command.session_id != self._active_session_id:
                self._begin_session(command.session_id, now_s=now_s)
            if command.action is SelectionAction.CANCEL_TRK:
                latest_sync = self._cancel_track(
                    bbox=command.bbox,
                    now_s=now_s,
                )
                continue
            self._active_command_id = command.command_id
            self._active_action = command.action
            mapped_track_id = self._command_to_track.get(command.command_id)
            if mapped_track_id is not None:
                self._active_track_id = mapped_track_id
            # ``OperatorTargetLock`` owns the legacy single-rectangle status
            # channel. Queue a manual TRK fallback as soon as a multi-TRK
            # command arrives, instead of waiting for that channel to acquire a
            # detector identity. This removes the one-frame/one-command race
            # where a moving selection was rejected before the next target-pool
            # update could create its local visual-tracker identity.
            if command.action is SelectionAction.SELECT_TRK and command.bbox is not None:
                candidate = self._candidate(
                    self.target_pool.snapshots(),
                    command.bbox,
                    preferred_label=None,
                )
                if candidate is not None:
                    latest_sync = self._bind_tracking(
                        command_id=command.command_id,
                        candidate=candidate,
                    )
                else:
                    self._queue_pending_manual_command(command)
                    latest_sync = self._sync(
                        pending_manual_observation=True,
                        reason="manual TRK fallback queued for the next target-pool frame",
                    )
            command_candidate = (
                self._candidate(self.target_pool.snapshots(), command.bbox, preferred_label=None)
                if command.bbox is not None
                and command.action in {SelectionAction.PROMOTE_LCK, SelectionAction.DEMOTE_TRK}
                else None
            )
            if command.action is SelectionAction.PROMOTE_LCK:
                candidate = command_candidate or (
                    self._snapshot_by_id(self._active_track_id)
                    if self._active_track_id is not None
                    else None
                )
                if candidate is not None:
                    latest_sync = self._promote(
                        command_id=command.command_id, candidate=candidate, now_s=now_s
                    )
            elif command.action is SelectionAction.DEMOTE_TRK:
                candidate = command_candidate or (
                    self._snapshot_by_id(self._active_track_id)
                    if self._active_track_id is not None
                    else None
                )
                if candidate is not None:
                    latest_sync = self._demote(
                        command_id=command.command_id, candidate=candidate, now_s=now_s
                    )
            status = self._latest_status(result.published_statuses, command.command_id)
            if status is not None:
                latest_sync = self._consume_active_status(status, now_s=now_s)

        if not result.accepted_selection_commands and self._active_command_id is not None:
            status = self._latest_status(
                result.published_statuses,
                self._active_command_id,
            )
            if status is not None:
                latest_sync = self._consume_active_status(status, now_s=now_s)
        return latest_sync

    def _consume_active_status(
        self,
        status: TrackStatusMessage,
        *,
        now_s: float,
    ) -> SelectionTargetPoolSync:
        if status.state is TrackingState.CANCELLED:
            self._pending.pop(status.selection_command_id, None)
            return self._sync(reason=f"operator tracking state is {status.state.value}")
        if status.state is TrackingState.LOST or status.bbox is None:
            # The legacy lock reports one active rectangle only. A transient
            # LOST/REJECTED there must not erase an independently selected TRK
            # identity or a manual fallback that the unified pool will bind on
            # the next frame. Explicit CANCEL/CANCEL_TRK remains the sole
            # removal path for operator selections.
            if (
                self._active_action is SelectionAction.SELECT_TRK
                and status.selection_command_id == self._active_command_id
                and (
                    status.selection_command_id in self._pending
                    or status.selection_command_id in self._command_to_track
                )
            ):
                return self._sync(
                    pending_manual_observation=bool(self._pending),
                    reason=(
                        "operator tracker is awaiting/recovering; "
                        "unified TRK selection remains active"
                    ),
                )
            return self._sync(reason="operator tracker is lost; target pool remains conservative")

        command_id = status.selection_command_id
        mapped_id = self._command_to_track.get(command_id)
        snapshots = self.target_pool.snapshots()
        mapped = next((track for track in snapshots if track.track_id == mapped_id), None)
        status_label = (status.label or "manual").strip().lower()
        if mapped is not None and status_label == "manual" and mapped.label == "manual":
            self._queue_pending_manual_observation(status)
            self._active_command_id = command_id
            self._active_track_id = mapped.track_id
            return self._sync(
                pending_manual_observation=True,
                reason="manual tracker observation queued for the next target-pool frame",
            )
        if mapped is not None:
            # The short-term tracker reports ``manual`` for a rectangle even when the
            # command was already associated with a detector identity.  Keep the
            # recognized identity authoritative rather than injecting a second track.
            self._pending.pop(command_id, None)
            self._active_command_id = command_id
            self._active_track_id = mapped.track_id
            return self._apply_interaction(command_id=command_id, candidate=mapped, now_s=now_s)

        preferred_label = None if status_label == "manual" else status_label
        candidate = self._candidate(
            snapshots,
            status.bbox,
            preferred_label=preferred_label,
        )
        if candidate is not None:
            activated = self._apply_interaction(
                command_id=command_id,
                candidate=candidate,
                now_s=now_s,
            )
            if activated.bound_track_id is not None:
                return activated

        self._active_command_id = command_id
        self._queue_pending_manual_observation(status)
        return self._sync(
            pending_manual_observation=True,
            reason="selection has no reliable existing target; manual observation queued",
        )

    def _apply_interaction(
        self,
        *,
        command_id: str,
        candidate: UnifiedTrackSnapshot,
        now_s: float,
        action: SelectionAction | None = None,
    ) -> SelectionTargetPoolSync:
        selected_action = action or self._active_action
        if selected_action is SelectionAction.SELECT_TRK:
            return self._bind_tracking(command_id=command_id, candidate=candidate)
        if selected_action is SelectionAction.DEMOTE_TRK:
            return self._demote(command_id=command_id, candidate=candidate, now_s=now_s)
        return self._promote(
            command_id=command_id,
            candidate=candidate,
            now_s=now_s,
            single_lock=selected_action is SelectionAction.PROMOTE_LCK,
        )

    def _bind_tracking(
        self,
        *,
        command_id: str,
        candidate: UnifiedTrackSnapshot,
    ) -> SelectionTargetPoolSync:
        previous_track_id = self._command_to_track.get(command_id)
        already_bound = (
            previous_track_id == candidate.track_id
            and candidate.track_id in self._tracked_track_ids
        )
        if previous_track_id is not None and previous_track_id != candidate.track_id:
            # A recognized detector identity has taken over a temporary manual
            # identity for this exact operator command.  Remove only the superseded
            # identity; other independently selected TRK targets remain untouched.
            self._tracked_track_ids.discard(previous_track_id)
        self._remember_mapping(command_id, candidate.track_id)
        self._active_command_id = command_id
        self._active_track_id = candidate.track_id
        self._tracked_track_ids.add(candidate.track_id)
        return self._sync(bound_track_id=None if already_bound else candidate.track_id)

    def _promote(
        self,
        *,
        command_id: str,
        candidate: UnifiedTrackSnapshot,
        now_s: float,
        single_lock: bool = True,
    ) -> SelectionTargetPoolSync:
        # A TRK selection may intentionally start from a broad/manual rectangle,
        # but LCK must wait for a confirmed, fresh target-pool identity. This
        # rejects a one-frame detector candidate before it can evict other TRK
        # targets and protects the exclusive high-rate path from weak matches.
        if single_lock and not candidate.actionable:
            return self._sync(
                pending_manual_observation=bool(self._pending),
                reason="candidate is not yet a stable tracking target",
            )
        if single_lock and candidate.confidence < self.config.minimum_lock_confidence:
            return self._sync(
                pending_manual_observation=bool(self._pending),
                reason="candidate confidence is below the LCK admission threshold",
            )
        if single_lock and candidate.tracking_quality < self.config.minimum_lock_tracking_quality:
            return self._sync(
                pending_manual_observation=bool(self._pending),
                reason="candidate tracking quality is below the LCK admission threshold",
            )
        if candidate.state in {
            UnifiedTrackState.OCCLUDED,
            UnifiedTrackState.REACQUIRING,
            UnifiedTrackState.LOST,
        }:
            return self._sync(
                pending_manual_observation=bool(self._pending),
                reason="candidate identity is uncertain and cannot become primary",
            )
        already_bound = self._command_to_track.get(command_id) == candidate.track_id
        if already_bound and candidate.locked and candidate.primary:
            if single_lock:
                for track in self.target_pool.snapshots():
                    if track.locked and track.track_id != candidate.track_id:
                        self.target_pool.unlock(track.track_id, now_s=now_s)
                self._tracked_track_ids = {candidate.track_id}
            self._active_command_id = command_id
            self._active_track_id = candidate.track_id
            return self._sync()
        try:
            if single_lock:
                for track in self.target_pool.snapshots():
                    if track.locked and track.track_id != candidate.track_id:
                        self.target_pool.unlock(track.track_id, now_s=now_s)
            if not candidate.locked:
                self.target_pool.lock(candidate.track_id, now_s=now_s)
            switched = self.target_pool.switch_primary(candidate.track_id, now_s=now_s)
        except ValueError as exc:
            return self._sync(
                pending_manual_observation=bool(self._pending),
                reason=str(exc),
            )
        self._remember_mapping(command_id, candidate.track_id)
        self._active_command_id = command_id
        self._active_track_id = candidate.track_id
        if single_lock:
            self._tracked_track_ids = {candidate.track_id}
        else:
            self._tracked_track_ids.add(candidate.track_id)
        return self._sync(
            bound_track_id=candidate.track_id,
            background_locked_track_ids=switched.background_locked_track_ids,
            primary_switch_latency_ms=switched.switch_latency_ms,
        )

    def _demote(
        self,
        *,
        command_id: str,
        candidate: UnifiedTrackSnapshot,
        now_s: float,
    ) -> SelectionTargetPoolSync:
        unlocked_id: str | None = None
        if candidate.locked:
            self.target_pool.unlock(candidate.track_id, now_s=now_s)
            unlocked_id = candidate.track_id
        self._remember_mapping(command_id, candidate.track_id)
        self._active_command_id = command_id
        self._active_track_id = candidate.track_id
        self._tracked_track_ids.add(candidate.track_id)
        return self._sync(
            bound_track_id=candidate.track_id,
            unlocked_track_id=unlocked_id,
        )

    def _cancel(self, *, now_s: float) -> SelectionTargetPoolSync:
        unlocked_id = self._active_track_id
        selected_ids = set(self._tracked_track_ids)
        if unlocked_id is not None:
            selected_ids.add(unlocked_id)
        for track_id in sorted(selected_ids):
            try:
                snapshot = self._snapshot_by_id(track_id)
                if snapshot is not None and snapshot.locked:
                    self.target_pool.unlock(track_id, now_s=now_s)
            except ValueError:
                if track_id == unlocked_id:
                    unlocked_id = None
        self._active_command_id = None
        self._active_track_id = None
        self._active_session_id = None
        self._tracked_track_ids.clear()
        self._active_action = None
        self._pending.clear()
        return self._sync(unlocked_track_id=unlocked_id)

    def _cancel_track(
        self,
        *,
        bbox: BoundingBox | None,
        now_s: float,
    ) -> SelectionTargetPoolSync:
        if bbox is None:
            return self._sync(reason="single-track cancel has no bounding box")
        candidate = self._candidate(
            self.target_pool.snapshots(),
            bbox,
            preferred_label=None,
        )
        if candidate is None:
            return self._sync(reason="single-track cancel did not match a target-pool track")
        if candidate.track_id not in self._tracked_track_ids:
            return self._sync(reason="single-track cancel matched a target outside operator TRK")

        unlocked_id: str | None = None
        if candidate.locked:
            try:
                self.target_pool.unlock(candidate.track_id, now_s=now_s)
                unlocked_id = candidate.track_id
            except ValueError:
                unlocked_id = None
        self._tracked_track_ids.discard(candidate.track_id)
        # A late singleton-status update for an individually cancelled target
        # must not resurrect its old operator command.  The remaining TRK
        # selections retain their own command-to-track mappings.
        self._forget_mappings_for_track(candidate.track_id)

        if self._active_track_id == candidate.track_id:
            replacement = self._preferred_active_track()
            self._active_track_id = replacement.track_id if replacement is not None else None
            self._active_command_id = (
                self._command_for_track(replacement.track_id) if replacement is not None else None
            )
            self._active_action = (
                SelectionAction.PROMOTE_LCK
                if replacement is not None and replacement.locked and replacement.primary
                else SelectionAction.SELECT_TRK
                if replacement is not None
                else None
            )
            self._pending = {
                command_id: pending
                for command_id, pending in self._pending.items()
                if self._command_to_track.get(command_id) != candidate.track_id
            }
        return self._sync(unlocked_track_id=unlocked_id)

    def _begin_session(self, session_id: str, *, now_s: float) -> None:
        """Drop selections left by an older QGC process before accepting a new session."""

        for track_id in sorted(self._tracked_track_ids):
            snapshot = self._snapshot_by_id(track_id)
            if snapshot is not None and snapshot.locked:
                try:
                    self.target_pool.unlock(track_id, now_s=now_s)
                except ValueError:
                    pass
        self._active_session_id = session_id
        self._active_command_id = None
        self._active_track_id = None
        self._active_action = None
        self._pending.clear()
        self._tracked_track_ids.clear()

    def _snapshot_by_id(self, track_id: str) -> UnifiedTrackSnapshot | None:
        return next(
            (track for track in self.target_pool.snapshots() if track.track_id == track_id),
            None,
        )

    def _preferred_active_track(self) -> UnifiedTrackSnapshot | None:
        candidates = [
            track
            for track in self.target_pool.snapshots()
            if track.track_id in self._tracked_track_ids
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda track: (
                -int(track.locked and track.primary),
                -int(track.locked),
                -track.tracking_quality,
                track.track_id,
            ),
        )

    def _command_for_track(self, track_id: str) -> str | None:
        return next(
            (
                command_id
                for command_id in reversed(self._mapping_order)
                if self._command_to_track.get(command_id) == track_id
            ),
            None,
        )

    def _candidate(
        self,
        tracks: tuple[UnifiedTrackSnapshot, ...],
        bbox: BoundingBox,
        *,
        preferred_label: str | None,
    ) -> UnifiedTrackSnapshot | None:
        selection_center_x, selection_center_y = bbox.center
        candidates: list[tuple[int, int, float, float, float, str, UnifiedTrackSnapshot]] = []
        for track in tracks:
            if track.state is UnifiedTrackState.LOST:
                continue
            if preferred_label is not None and track.label != preferred_label:
                continue
            center_x, center_y = track.bbox.center
            center_distance = hypot(
                center_x - selection_center_x,
                center_y - selection_center_y,
            )
            overlap = bbox.iou(track.bbox)
            center_inside = bbox.x1 <= center_x <= bbox.x2 and bbox.y1 <= center_y <= bbox.y2
            if overlap < self.config.minimum_iou and not center_inside:
                continue
            if not center_inside and center_distance > self.config.maximum_center_distance:
                continue
            candidates.append(
                (
                    # Detector-backed identities carry class-specific detector/ReID
                    # support.  Prefer them over the temporary manual fallback when
                    # both overlap the same operator selection.
                    int(track.label != "manual"),
                    int(center_inside),
                    overlap,
                    center_distance,
                    track.tracking_quality,
                    track.track_id,
                    track,
                )
            )
        if not candidates:
            return None
        candidates.sort(
            key=lambda item: (
                -item[0],
                -item[1],
                -item[2],
                item[3],
                -item[4],
                item[5],
            )
        )
        if len(candidates) > 1:
            first, second = candidates[:2]
            ambiguous = (
                first[0] == second[0]
                and first[1] == second[1]
                and abs(first[2] - second[2]) <= self.config.ambiguity_iou_margin
                and abs(first[3] - second[3]) <= self.config.ambiguity_center_distance_margin
            )
            if ambiguous:
                return None
        return candidates[0][-1]

    def _queue_pending_manual_observation(self, status: TrackStatusMessage) -> None:
        self._queue_pending_manual(self._manual_observation(status))

    def _queue_pending_manual_command(self, command: TargetSelectionCommand) -> None:
        if command.bbox is None:
            raise ValueError("manual TRK fallback requires a bounding box")
        self._queue_pending_manual(
            _PendingManualObservation(
                command_id=command.command_id,
                status_target_id=f"pending:{command.command_id}",
                bbox=command.bbox,
                confidence=self.config.manual_observation_confidence,
                action=SelectionAction.SELECT_TRK,
            )
        )

    def _queue_pending_manual(self, pending: _PendingManualObservation) -> None:
        if pending.command_id not in self._pending:
            while len(self._pending) >= self.config.mapping_history_size:
                oldest_command_id = next(iter(self._pending))
                self._pending.pop(oldest_command_id)
        self._pending[pending.command_id] = pending

    def _manual_observation(self, status: TrackStatusMessage) -> _PendingManualObservation:
        if status.bbox is None or status.target_id is None:
            raise ValueError("active manual status requires target ID and bounding box")
        confidence_candidates = tuple(
            value for value in (status.confidence, status.tracking_quality) if value is not None
        )
        confidence = max((self.config.manual_observation_confidence, *confidence_candidates))
        confidence = max(0.05, min(1.0, confidence))
        return _PendingManualObservation(
            command_id=status.selection_command_id,
            status_target_id=status.target_id,
            bbox=status.bbox,
            confidence=confidence,
            action=self._active_action or SelectionAction.SELECT,
        )

    def _remember_mapping(self, command_id: str, track_id: str) -> None:
        if command_id not in self._command_to_track:
            if len(self._mapping_order) >= self.config.mapping_history_size:
                oldest = self._mapping_order.popleft()
                self._command_to_track.pop(oldest, None)
            self._mapping_order.append(command_id)
        self._command_to_track[command_id] = track_id

    def _forget_mappings_for_track(self, track_id: str) -> None:
        command_ids = tuple(
            command_id
            for command_id, mapped_track_id in self._command_to_track.items()
            if mapped_track_id == track_id
        )
        for command_id in command_ids:
            self._command_to_track.pop(command_id, None)
            self._pending.pop(command_id, None)
            try:
                self._mapping_order.remove(command_id)
            except ValueError:
                pass

    def _sync(
        self,
        *,
        bound_track_id: str | None = None,
        unlocked_track_id: str | None = None,
        pending_manual_observation: bool | None = None,
        background_locked_track_ids: tuple[str, ...] = (),
        primary_switch_latency_ms: float | None = None,
        reason: str | None = None,
    ) -> SelectionTargetPoolSync:
        return SelectionTargetPoolSync(
            active_selection_command_id=self._active_command_id,
            active_track_id=self._active_track_id,
            tracked_track_ids=self.tracked_track_ids,
            bound_track_id=bound_track_id,
            unlocked_track_id=unlocked_track_id,
            pending_manual_observation=(
                bool(self._pending)
                if pending_manual_observation is None
                else pending_manual_observation
            ),
            background_locked_track_ids=background_locked_track_ids,
            primary_switch_latency_ms=primary_switch_latency_ms,
            reason=reason,
        )

    @staticmethod
    def _latest_status(
        statuses: tuple[TrackStatusMessage, ...],
        command_id: str,
    ) -> TrackStatusMessage | None:
        return next(
            (status for status in reversed(statuses) if status.selection_command_id == command_id),
            None,
        )

    @staticmethod
    def _validate_now(now_s: float) -> None:
        if not isfinite(now_s) or now_s < 0.0:
            raise ValueError("selection target-pool timestamp must be finite and non-negative")


__all__ = [
    "SelectionTargetPoolConfig",
    "SelectionTargetPoolSync",
    "UnifiedSelectionTargetPool",
]
