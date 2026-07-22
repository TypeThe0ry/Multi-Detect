from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from math import isfinite
from typing import Any, Protocol

from .domain import TrackSnapshot
from .manual_tracking import OpenCVManualTargetTracker
from .operator_link import (
    ApproachChallengeStatusMessage,
    ApproachConfirmationCommand,
    ApproachStatusMessage,
    AuthorizationChallengeStatusMessage,
    AuthorizationDecisionCommand,
    MissionStatusMessage,
    PatrolStatusMessage,
    PayloadTargetChallengeStatusMessage,
    PayloadTargetConfirmationCommand,
    PayloadTargetStatusMessage,
    RangeStatusMessage,
    ReleaseStatusMessage,
    SafetyStatusMessage,
    SceneContextStatusMessage,
    SelectionAction,
    TargetGeolocationStatusMessage,
    TargetPoolStatusMessage,
    TargetSelectionCommand,
    TrackingState,
    TrackStatusMessage,
    VideoGeometry,
)
from .operator_tracking import OperatorTargetLock

OperatorPeer = tuple[str, int]


class OperatorStatusTransport(Protocol):
    def start_background(self) -> None: ...

    def active_metadata_peer(self) -> OperatorPeer | None: ...

    def poll_selection(self) -> tuple[TargetSelectionCommand, OperatorPeer] | None: ...

    def poll_authorization_decision(
        self,
    ) -> tuple[AuthorizationDecisionCommand, OperatorPeer] | None: ...

    def poll_approach_confirmation(
        self,
    ) -> tuple[ApproachConfirmationCommand, OperatorPeer] | None: ...

    def poll_payload_target_confirmation(
        self,
    ) -> tuple[PayloadTargetConfirmationCommand, OperatorPeer] | None: ...

    def set_authorization_challenge(
        self,
        status: AuthorizationChallengeStatusMessage | None,
    ) -> None: ...

    def set_approach_challenge(
        self,
        status: ApproachChallengeStatusMessage | None,
    ) -> None: ...

    def set_payload_target_challenge(
        self,
        status: PayloadTargetChallengeStatusMessage | None,
    ) -> None: ...

    def poll_error(self) -> Exception | None: ...

    def publish_track_status(
        self,
        status: TrackStatusMessage,
        *,
        peer: OperatorPeer,
    ) -> None: ...

    def publish_mission_status(
        self,
        status: MissionStatusMessage,
        *,
        peer: OperatorPeer,
    ) -> None: ...

    def publish_safety_status(
        self,
        status: SafetyStatusMessage,
        *,
        peer: OperatorPeer,
    ) -> None: ...

    def publish_patrol_status(
        self,
        status: PatrolStatusMessage,
        *,
        peer: OperatorPeer,
    ) -> None: ...

    def publish_range_status(
        self,
        status: RangeStatusMessage,
        *,
        peer: OperatorPeer,
    ) -> None: ...

    def publish_target_geolocation_status(
        self,
        status: TargetGeolocationStatusMessage,
        *,
        peer: OperatorPeer,
    ) -> None: ...

    def publish_release_status(
        self,
        status: ReleaseStatusMessage,
        *,
        peer: OperatorPeer,
    ) -> None: ...

    def publish_authorization_challenge(
        self,
        status: AuthorizationChallengeStatusMessage,
        *,
        peer: OperatorPeer,
    ) -> None: ...

    def publish_approach_challenge(
        self,
        status: ApproachChallengeStatusMessage,
        *,
        peer: OperatorPeer,
    ) -> None: ...

    def publish_approach_status(
        self,
        status: ApproachStatusMessage,
        *,
        peer: OperatorPeer,
    ) -> None: ...

    def publish_payload_target_challenge(
        self,
        status: PayloadTargetChallengeStatusMessage,
        *,
        peer: OperatorPeer,
    ) -> None: ...

    def publish_payload_target_status(
        self,
        status: PayloadTargetStatusMessage,
        *,
        peer: OperatorPeer,
    ) -> None: ...

    def publish_target_pool_status(
        self,
        status: TargetPoolStatusMessage,
        *,
        peer: OperatorPeer,
    ) -> None: ...

    def publish_scene_context_status(
        self,
        status: SceneContextStatusMessage,
        *,
        peer: OperatorPeer,
    ) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class OperatorBridgeResult:
    accepted_command_count: int
    published_statuses: tuple[TrackStatusMessage, ...]
    published_mission_statuses: tuple[MissionStatusMessage, ...]
    published_safety_statuses: tuple[SafetyStatusMessage, ...]
    accepted_authorization_decisions: tuple[tuple[AuthorizationDecisionCommand, OperatorPeer], ...]
    published_authorization_challenges: tuple[AuthorizationChallengeStatusMessage, ...]
    transport_errors: tuple[str, ...]
    accepted_selection_commands: tuple[tuple[TargetSelectionCommand, OperatorPeer], ...] = ()
    published_patrol_statuses: tuple[PatrolStatusMessage, ...] = ()
    published_range_statuses: tuple[RangeStatusMessage, ...] = ()
    published_target_geolocation_statuses: tuple[TargetGeolocationStatusMessage, ...] = ()
    published_release_statuses: tuple[ReleaseStatusMessage, ...] = ()
    accepted_approach_confirmations: tuple[
        tuple[ApproachConfirmationCommand, OperatorPeer], ...
    ] = ()
    published_approach_challenges: tuple[ApproachChallengeStatusMessage, ...] = ()
    published_approach_statuses: tuple[ApproachStatusMessage, ...] = ()
    accepted_payload_target_confirmations: tuple[
        tuple[PayloadTargetConfirmationCommand, OperatorPeer], ...
    ] = ()
    published_payload_target_challenges: tuple[PayloadTargetChallengeStatusMessage, ...] = ()
    published_payload_target_statuses: tuple[PayloadTargetStatusMessage, ...] = ()
    published_target_pool_statuses: tuple[TargetPoolStatusMessage, ...] = ()
    published_scene_context_statuses: tuple[SceneContextStatusMessage, ...] = ()


class LiveOperatorBridge:
    """Join remote selection/status and bounded authorization metadata, never actuator control."""

    def __init__(
        self,
        transport: OperatorStatusTransport,
        target_lock: OperatorTargetLock,
        *,
        maximum_commands_per_frame: int = 8,
        maximum_errors_per_frame: int = 8,
        mission_status_heartbeat_s: float = 1.0,
        range_status_heartbeat_s: float = 1.0 / 15.0,
        target_geolocation_status_heartbeat_s: float = 1.0 / 15.0,
        release_status_heartbeat_s: float = 1.0 / 15.0,
        approach_status_heartbeat_s: float = 1.0 / 15.0,
        payload_target_status_heartbeat_s: float = 1.0 / 15.0,
        target_pool_status_heartbeat_s: float = 0.2,
        scene_context_status_heartbeat_s: float = 0.5,
        manual_tracker_factory: Callable[[VideoGeometry], Any] | None = None,
    ) -> None:
        if maximum_commands_per_frame <= 0 or maximum_errors_per_frame <= 0:
            raise ValueError("operator bridge per-frame limits must be positive")
        if not isfinite(mission_status_heartbeat_s) or mission_status_heartbeat_s <= 0.0:
            raise ValueError("mission status heartbeat must be finite and positive")
        if not isfinite(range_status_heartbeat_s) or range_status_heartbeat_s <= 0.0:
            raise ValueError("range status heartbeat must be finite and positive")
        if (
            not isfinite(target_geolocation_status_heartbeat_s)
            or target_geolocation_status_heartbeat_s <= 0.0
        ):
            raise ValueError("target geolocation status heartbeat must be finite and positive")
        if not isfinite(release_status_heartbeat_s) or release_status_heartbeat_s <= 0.0:
            raise ValueError("release status heartbeat must be finite and positive")
        if not isfinite(approach_status_heartbeat_s) or approach_status_heartbeat_s <= 0.0:
            raise ValueError("approach status heartbeat must be finite and positive")
        if (
            not isfinite(payload_target_status_heartbeat_s)
            or payload_target_status_heartbeat_s <= 0.0
        ):
            raise ValueError("payload target status heartbeat must be finite and positive")
        if not isfinite(target_pool_status_heartbeat_s) or target_pool_status_heartbeat_s <= 0.0:
            raise ValueError("target-pool status heartbeat must be finite and positive")
        if (
            not isfinite(scene_context_status_heartbeat_s)
            or scene_context_status_heartbeat_s <= 0.0
        ):
            raise ValueError("scene-context status heartbeat must be finite and positive")
        self.transport = transport
        self.target_lock = target_lock
        self.maximum_commands_per_frame = maximum_commands_per_frame
        self.maximum_errors_per_frame = maximum_errors_per_frame
        self.mission_status_heartbeat_s = mission_status_heartbeat_s
        self.range_status_heartbeat_s = range_status_heartbeat_s
        self.target_geolocation_status_heartbeat_s = target_geolocation_status_heartbeat_s
        self.release_status_heartbeat_s = release_status_heartbeat_s
        self.approach_status_heartbeat_s = approach_status_heartbeat_s
        self.payload_target_status_heartbeat_s = payload_target_status_heartbeat_s
        self.target_pool_status_heartbeat_s = target_pool_status_heartbeat_s
        self.scene_context_status_heartbeat_s = scene_context_status_heartbeat_s
        self._manual_tracker_factory = manual_tracker_factory or OpenCVManualTargetTracker
        self._manual_tracker: Any | None = None
        self._manual_tracker_unavailable = False
        self._active_selection_command: TargetSelectionCommand | None = None
        self._active_peer: OperatorPeer | None = None
        self._last_mission_status_fingerprint: tuple[object, ...] | None = None
        self._last_mission_status_at_s: float | None = None
        self._last_safety_status_fingerprint: tuple[object, ...] | None = None
        self._last_safety_status_at_s: float | None = None
        self._last_patrol_status_fingerprint: tuple[object, ...] | None = None
        self._last_patrol_status_at_s: float | None = None
        self._last_range_status_fingerprint: tuple[object, ...] | None = None
        self._last_range_status_at_s: float | None = None
        self._last_target_geolocation_status_fingerprint: tuple[object, ...] | None = None
        self._last_target_geolocation_status_at_s: float | None = None
        self._last_release_status_fingerprint: tuple[object, ...] | None = None
        self._last_release_status_at_s: float | None = None
        self._last_approach_challenge_fingerprint: tuple[object, ...] | None = None
        self._last_approach_challenge_at_s: float | None = None
        self._last_approach_status_fingerprint: tuple[object, ...] | None = None
        self._last_approach_status_at_s: float | None = None
        self._last_payload_target_challenge_fingerprint: tuple[object, ...] | None = None
        self._last_payload_target_challenge_at_s: float | None = None
        self._last_payload_target_status_fingerprint: tuple[object, ...] | None = None
        self._last_payload_target_status_at_s: float | None = None
        self._last_target_pool_revision: int | None = None
        self._last_target_pool_at_s: float | None = None
        self._last_scene_context_revision: int | None = None
        self._last_scene_context_at_s: float | None = None
        self._last_authorization_challenge_fingerprint: tuple[object, ...] | None = None
        self._last_authorization_challenge_at_s: float | None = None
        self._started = False

    def start(self) -> None:
        if self._started:
            raise RuntimeError("live operator bridge is already started")
        self.transport.start_background()
        self._started = True

    @property
    def active_peer(self) -> OperatorPeer | None:
        """Latest authenticated operator metadata recipient, if its lease is live."""

        return self._active_peer

    def process_frame(
        self,
        *,
        tracks: tuple[TrackSnapshot, ...],
        frame_id: str,
        captured_at_s: float,
        produced_at_s: float,
        mission_status: MissionStatusMessage | None = None,
        safety_status: SafetyStatusMessage | None = None,
        patrol_status: PatrolStatusMessage | None = None,
        range_status: RangeStatusMessage | None = None,
        target_geolocation_status: TargetGeolocationStatusMessage | None = None,
        release_status: ReleaseStatusMessage | None = None,
        approach_challenge: ApproachChallengeStatusMessage | None = None,
        approach_status: ApproachStatusMessage | None = None,
        payload_target_challenge: PayloadTargetChallengeStatusMessage | None = None,
        payload_target_status: PayloadTargetStatusMessage | None = None,
        target_pool_statuses: tuple[TargetPoolStatusMessage, ...] = (),
        scene_context_statuses: tuple[SceneContextStatusMessage, ...] = (),
        authorization_challenge: AuthorizationChallengeStatusMessage | None = None,
        image_bgr: Any | None = None,
    ) -> OperatorBridgeResult:
        if not self._started:
            raise RuntimeError("live operator bridge has not been started")
        errors = self._drain_errors()
        active_metadata_peer = getattr(self.transport, "active_metadata_peer", None)
        if callable(active_metadata_peer):
            try:
                discovered_peer = active_metadata_peer()
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                errors.append(type(exc).__name__)
            else:
                if discovered_peer != self._active_peer:
                    self._set_active_peer(discovered_peer)
        statuses: list[TrackStatusMessage] = []
        accepted_commands = 0
        accepted_selection_commands: list[tuple[TargetSelectionCommand, OperatorPeer]] = []
        for _ in range(self.maximum_commands_per_frame):
            queued = self.transport.poll_selection()
            if queued is None:
                break
            command, peer = queued
            accepted_commands += 1
            accepted_selection_commands.append((command, peer))
            self._set_active_peer(peer, force_reset=True)
            active_track_before = self.target_lock.active_track_id
            active_selection_before = self._active_selection_command
            detector_status = self.target_lock.apply_command(
                command,
                tracks=tracks,
                frame_id=frame_id,
                now_s=produced_at_s,
            )
            status = detector_status
            if command.action is SelectionAction.CANCEL:
                self._active_selection_command = None
                if self._manual_tracker is not None:
                    try:
                        self._manual_tracker.apply_command(
                            command,
                            image_bgr=image_bgr if image_bgr is not None else object(),
                            frame_id=frame_id,
                            now_s=produced_at_s,
                        )
                    except (OSError, RuntimeError, TypeError, ValueError) as exc:
                        errors.append(f"ManualTracker{type(exc).__name__}")
                self._manual_tracker = None
                self._manual_tracker_unavailable = False
            elif command.action is SelectionAction.CANCEL_TRK:
                active_selection_bbox = (
                    active_selection_before.bbox if active_selection_before is not None else None
                )
                command_bbox = command.bbox
                cancels_current = (
                    active_track_before is not None
                    and detector_status.target_id == active_track_before
                ) or (
                    active_selection_bbox is not None
                    and command_bbox is not None
                    and (
                        active_selection_bbox.iou(command_bbox) > 0.05
                        or (
                            command_bbox.x1
                            <= active_selection_bbox.center[0]
                            <= command_bbox.x2
                            and command_bbox.y1
                            <= active_selection_bbox.center[1]
                            <= command_bbox.y2
                        )
                    )
                )
                if cancels_current:
                    self._active_selection_command = None
                    self._manual_tracker = None
                    self._manual_tracker_unavailable = False
            elif image_bgr is not None:
                self._active_selection_command = command
                self._manual_tracker_unavailable = False
                try:
                    manual_command = command
                    if (
                        detector_status.state is TrackingState.TRACKING
                        and detector_status.bbox is not None
                    ):
                        # The operator rectangle can be intentionally broad.  Once the
                        # detector has associated it with a target, seed the shadow
                        # tracker from the tighter detector box so it is ready for a
                        # clean handoff if detector observations disappear.
                        manual_command = replace(command, bbox=detector_status.bbox)
                    manual_tracker = self._manual_tracker_factory(manual_command.geometry)
                    manual_status = manual_tracker.apply_command(
                        manual_command,
                        image_bgr=image_bgr,
                        frame_id=frame_id,
                        now_s=produced_at_s,
                    )
                    if manual_status.state in {
                        TrackingState.TRACKING,
                        TrackingState.INITIALIZING,
                    }:
                        self._manual_tracker = manual_tracker
                        if detector_status.state is not TrackingState.TRACKING:
                            status = manual_status
                    else:
                        self._manual_tracker = None
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    self._manual_tracker = None
                    self._manual_tracker_unavailable = True
                    errors.append(f"ManualTracker{type(exc).__name__}")
            else:
                self._active_selection_command = command
                self._manual_tracker = None
                self._manual_tracker_unavailable = False
            if self._publish(status, peer, errors):
                statuses.append(status)
        set_challenge = getattr(self.transport, "set_authorization_challenge", None)
        if callable(set_challenge) and authorization_challenge is None:
            try:
                set_challenge(None)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                errors.append(type(exc).__name__)
        accepted_authorization_decisions: list[
            tuple[AuthorizationDecisionCommand, OperatorPeer]
        ] = []
        poll_decision = getattr(self.transport, "poll_authorization_decision", None)
        if callable(poll_decision):
            for _ in range(self.maximum_commands_per_frame):
                queued_decision = poll_decision()
                if queued_decision is None:
                    break
                command, peer = queued_decision
                if self._active_peer is None or peer != self._active_peer:
                    errors.append("AuthorizationPeerMismatch")
                    continue
                accepted_authorization_decisions.append((command, peer))
        # A selection change invalidates every target/revision-bound Mode-3
        # challenge before any confirmation is polled or status is published.
        effective_approach_challenge = None if accepted_selection_commands else approach_challenge
        effective_approach_status = None if accepted_selection_commands else approach_status
        set_approach_challenge = getattr(self.transport, "set_approach_challenge", None)
        if callable(set_approach_challenge) and effective_approach_challenge is None:
            try:
                set_approach_challenge(None)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                errors.append(type(exc).__name__)
        accepted_approach_confirmations: list[tuple[ApproachConfirmationCommand, OperatorPeer]] = []
        poll_approach = getattr(self.transport, "poll_approach_confirmation", None)
        if callable(poll_approach) and effective_approach_challenge is not None:
            for _ in range(self.maximum_commands_per_frame):
                queued_approach = poll_approach()
                if queued_approach is None:
                    break
                command, peer = queued_approach
                if self._active_peer is None or peer != self._active_peer:
                    errors.append("ApproachPeerMismatch")
                    continue
                accepted_approach_confirmations.append((command, peer))
        effective_payload_target_challenge = (
            None if accepted_selection_commands else payload_target_challenge
        )
        effective_payload_target_status = (
            None if accepted_selection_commands else payload_target_status
        )
        set_payload_target_challenge = getattr(
            self.transport,
            "set_payload_target_challenge",
            None,
        )
        if callable(set_payload_target_challenge) and effective_payload_target_challenge is None:
            try:
                set_payload_target_challenge(None)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                errors.append(type(exc).__name__)
        accepted_payload_target_confirmations: list[
            tuple[PayloadTargetConfirmationCommand, OperatorPeer]
        ] = []
        poll_payload_target = getattr(
            self.transport,
            "poll_payload_target_confirmation",
            None,
        )
        if callable(poll_payload_target) and effective_payload_target_challenge is not None:
            for _ in range(self.maximum_commands_per_frame):
                queued_payload_target = poll_payload_target()
                if queued_payload_target is None:
                    break
                command, peer = queued_payload_target
                if self._active_peer is None or peer != self._active_peer:
                    errors.append("PayloadTargetPeerMismatch")
                    continue
                accepted_payload_target_confirmations.append((command, peer))
        if accepted_commands == 0 and self._active_peer is not None:
            detector_status = self.target_lock.update(
                tracks=tracks,
                frame_id=frame_id,
                captured_at_s=captured_at_s,
                produced_at_s=produced_at_s,
            )
            status = detector_status
            manual_status = None
            if self._manual_tracker is not None and image_bgr is not None:
                try:
                    manual_status = self._manual_tracker.update(
                        image_bgr=image_bgr,
                        frame_id=frame_id,
                        captured_at_s=captured_at_s,
                        produced_at_s=produced_at_s,
                    )
                    if manual_status is not None and manual_status.state is TrackingState.LOST:
                        self._manual_tracker = None
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    self._manual_tracker = None
                    self._manual_tracker_unavailable = True
                    errors.append(f"ManualTracker{type(exc).__name__}")
            detector_tracking = (
                detector_status is not None and detector_status.state is TrackingState.TRACKING
            )
            if (
                detector_tracking
                and self._manual_tracker is None
                and not self._manual_tracker_unavailable
                and self._active_selection_command is not None
                and detector_status is not None
                and detector_status.bbox is not None
                and image_bgr is not None
            ):
                try:
                    shadow_command = replace(
                        self._active_selection_command,
                        bbox=detector_status.bbox,
                    )
                    manual_tracker = self._manual_tracker_factory(shadow_command.geometry)
                    shadow_status = manual_tracker.apply_command(
                        shadow_command,
                        image_bgr=image_bgr,
                        frame_id=frame_id,
                        now_s=produced_at_s,
                    )
                    if shadow_status.state is TrackingState.TRACKING:
                        self._manual_tracker = manual_tracker
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    self._manual_tracker = None
                    self._manual_tracker_unavailable = True
                    errors.append(f"ManualTracker{type(exc).__name__}")
            if not detector_tracking and manual_status is not None:
                status = manual_status
                if manual_status.bbox is not None and manual_status.state in {
                    TrackingState.TRACKING,
                    TrackingState.INITIALIZING,
                }:
                    self.target_lock.hint_bbox(
                        manual_status.bbox,
                        now_s=produced_at_s,
                    )
            if status is not None and self._publish(status, self._active_peer, errors):
                statuses.append(status)
        published_mission_statuses: tuple[MissionStatusMessage, ...] = ()
        if (
            mission_status is not None
            and self._active_peer is not None
            and self._mission_status_due(mission_status)
            and self._publish_mission_status(mission_status, self._active_peer, errors)
        ):
            published_mission_statuses = (mission_status,)
            self._last_mission_status_fingerprint = self._mission_status_fingerprint(mission_status)
            self._last_mission_status_at_s = mission_status.produced_at_s
        published_safety_statuses: tuple[SafetyStatusMessage, ...] = ()
        if (
            safety_status is not None
            and self._active_peer is not None
            and self._safety_status_due(safety_status)
            and self._publish_safety_status(safety_status, self._active_peer, errors)
        ):
            published_safety_statuses = (safety_status,)
            self._last_safety_status_fingerprint = self._safety_status_fingerprint(safety_status)
            self._last_safety_status_at_s = safety_status.produced_at_s
        published_patrol_statuses: tuple[PatrolStatusMessage, ...] = ()
        if (
            patrol_status is not None
            and self._active_peer is not None
            and self._patrol_status_due(patrol_status)
            and self._publish_patrol_status(patrol_status, self._active_peer, errors)
        ):
            published_patrol_statuses = (patrol_status,)
            self._last_patrol_status_fingerprint = self._patrol_status_fingerprint(patrol_status)
            self._last_patrol_status_at_s = patrol_status.produced_at_s
        published_range_statuses: tuple[RangeStatusMessage, ...] = ()
        if (
            range_status is not None
            and self._active_peer is not None
            and self._range_status_due(range_status)
            and self._publish_range_status(range_status, self._active_peer, errors)
        ):
            published_range_statuses = (range_status,)
            self._last_range_status_fingerprint = self._range_status_fingerprint(range_status)
            self._last_range_status_at_s = range_status.produced_at_s
        published_target_geolocation_statuses: tuple[TargetGeolocationStatusMessage, ...] = ()
        if (
            target_geolocation_status is not None
            and self._active_peer is not None
            and self._target_geolocation_status_due(target_geolocation_status)
            and self._publish_target_geolocation_status(
                target_geolocation_status,
                self._active_peer,
                errors,
            )
        ):
            published_target_geolocation_statuses = (target_geolocation_status,)
            self._last_target_geolocation_status_fingerprint = (
                self._target_geolocation_status_fingerprint(target_geolocation_status)
            )
            self._last_target_geolocation_status_at_s = target_geolocation_status.produced_at_s
        published_release_statuses: tuple[ReleaseStatusMessage, ...] = ()
        if (
            release_status is not None
            and self._active_peer is not None
            and self._release_status_due(release_status)
            and self._publish_release_status(release_status, self._active_peer, errors)
        ):
            published_release_statuses = (release_status,)
            self._last_release_status_fingerprint = self._release_status_fingerprint(release_status)
            self._last_release_status_at_s = release_status.produced_at_s
        published_approach_challenges: tuple[ApproachChallengeStatusMessage, ...] = ()
        if (
            effective_approach_challenge is not None
            and self._active_peer is not None
            and self._approach_challenge_due(effective_approach_challenge)
            and self._publish_approach_challenge(
                effective_approach_challenge, self._active_peer, errors
            )
        ):
            published_approach_challenges = (effective_approach_challenge,)
            self._last_approach_challenge_fingerprint = self._approach_challenge_fingerprint(
                effective_approach_challenge
            )
            self._last_approach_challenge_at_s = effective_approach_challenge.produced_at_s
            if callable(set_approach_challenge):
                try:
                    set_approach_challenge(effective_approach_challenge)
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    errors.append(type(exc).__name__)
        published_approach_statuses: tuple[ApproachStatusMessage, ...] = ()
        if (
            effective_approach_status is not None
            and self._active_peer is not None
            and self._approach_status_due(effective_approach_status)
            and self._publish_approach_status(effective_approach_status, self._active_peer, errors)
        ):
            published_approach_statuses = (effective_approach_status,)
            self._last_approach_status_fingerprint = self._approach_status_fingerprint(
                effective_approach_status
            )
            self._last_approach_status_at_s = effective_approach_status.produced_at_s
        published_payload_target_challenges: tuple[PayloadTargetChallengeStatusMessage, ...] = ()
        if (
            effective_payload_target_challenge is not None
            and self._active_peer is not None
            and self._payload_target_challenge_due(effective_payload_target_challenge)
            and self._publish_payload_target_challenge(
                effective_payload_target_challenge,
                self._active_peer,
                errors,
            )
        ):
            published_payload_target_challenges = (effective_payload_target_challenge,)
            self._last_payload_target_challenge_fingerprint = (
                self._payload_target_challenge_fingerprint(effective_payload_target_challenge)
            )
            self._last_payload_target_challenge_at_s = (
                effective_payload_target_challenge.produced_at_s
            )
            if callable(set_payload_target_challenge):
                try:
                    set_payload_target_challenge(effective_payload_target_challenge)
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    errors.append(type(exc).__name__)
        published_payload_target_statuses: tuple[PayloadTargetStatusMessage, ...] = ()
        if (
            effective_payload_target_status is not None
            and self._active_peer is not None
            and self._payload_target_status_due(effective_payload_target_status)
            and self._publish_payload_target_status(
                effective_payload_target_status,
                self._active_peer,
                errors,
            )
        ):
            published_payload_target_statuses = (effective_payload_target_status,)
            self._last_payload_target_status_fingerprint = self._payload_target_status_fingerprint(
                effective_payload_target_status
            )
            self._last_payload_target_status_at_s = effective_payload_target_status.produced_at_s
        published_target_pool_statuses: tuple[TargetPoolStatusMessage, ...] = ()
        if (
            target_pool_statuses
            and self._active_peer is not None
            and self._target_pool_status_due(target_pool_statuses)
        ):
            published: list[TargetPoolStatusMessage] = []
            for status in target_pool_statuses:
                if self._publish_target_pool_status(status, self._active_peer, errors):
                    published.append(status)
            if len(published) == len(target_pool_statuses):
                published_target_pool_statuses = tuple(published)
                self._last_target_pool_revision = target_pool_statuses[0].pool_revision
                self._last_target_pool_at_s = target_pool_statuses[0].produced_at_s
        published_scene_context_statuses: tuple[SceneContextStatusMessage, ...] = ()
        if (
            scene_context_statuses
            and self._active_peer is not None
            and self._scene_context_status_due(scene_context_statuses)
        ):
            published_context: list[SceneContextStatusMessage] = []
            for status in scene_context_statuses:
                if self._publish_scene_context_status(status, self._active_peer, errors):
                    published_context.append(status)
            if len(published_context) == len(scene_context_statuses):
                published_scene_context_statuses = tuple(published_context)
                self._last_scene_context_revision = scene_context_statuses[0].context_revision
                self._last_scene_context_at_s = scene_context_statuses[0].produced_at_s
        published_authorization_challenges: tuple[AuthorizationChallengeStatusMessage, ...] = ()
        if (
            authorization_challenge is not None
            and self._active_peer is not None
            and self._authorization_challenge_due(authorization_challenge)
            and self._publish_authorization_challenge(
                authorization_challenge,
                self._active_peer,
                errors,
            )
        ):
            published_authorization_challenges = (authorization_challenge,)
            self._last_authorization_challenge_fingerprint = (
                self._authorization_challenge_fingerprint(authorization_challenge)
            )
            self._last_authorization_challenge_at_s = authorization_challenge.produced_at_s
            if callable(set_challenge):
                try:
                    set_challenge(authorization_challenge)
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    errors.append(type(exc).__name__)
        return OperatorBridgeResult(
            accepted_command_count=accepted_commands,
            published_statuses=tuple(statuses),
            published_mission_statuses=published_mission_statuses,
            published_safety_statuses=published_safety_statuses,
            published_patrol_statuses=published_patrol_statuses,
            published_range_statuses=published_range_statuses,
            published_target_geolocation_statuses=published_target_geolocation_statuses,
            published_release_statuses=published_release_statuses,
            accepted_approach_confirmations=tuple(accepted_approach_confirmations),
            published_approach_challenges=published_approach_challenges,
            published_approach_statuses=published_approach_statuses,
            accepted_payload_target_confirmations=tuple(accepted_payload_target_confirmations),
            published_payload_target_challenges=published_payload_target_challenges,
            published_payload_target_statuses=published_payload_target_statuses,
            published_target_pool_statuses=published_target_pool_statuses,
            published_scene_context_statuses=published_scene_context_statuses,
            accepted_authorization_decisions=tuple(accepted_authorization_decisions),
            published_authorization_challenges=published_authorization_challenges,
            transport_errors=tuple(errors),
            accepted_selection_commands=tuple(accepted_selection_commands),
        )

    def _set_active_peer(
        self,
        peer: OperatorPeer | None,
        *,
        force_reset: bool = False,
    ) -> None:
        if not force_reset and peer == self._active_peer:
            return
        self._active_peer = peer
        self._last_mission_status_fingerprint = None
        self._last_mission_status_at_s = None
        self._last_safety_status_fingerprint = None
        self._last_safety_status_at_s = None
        self._last_patrol_status_fingerprint = None
        self._last_patrol_status_at_s = None
        self._last_range_status_fingerprint = None
        self._last_range_status_at_s = None
        self._last_target_geolocation_status_fingerprint = None
        self._last_target_geolocation_status_at_s = None
        self._last_release_status_fingerprint = None
        self._last_release_status_at_s = None
        self._last_approach_challenge_fingerprint = None
        self._last_approach_challenge_at_s = None
        self._last_approach_status_fingerprint = None
        self._last_approach_status_at_s = None
        self._last_payload_target_challenge_fingerprint = None
        self._last_payload_target_challenge_at_s = None
        self._last_payload_target_status_fingerprint = None
        self._last_payload_target_status_at_s = None
        self._last_target_pool_revision = None
        self._last_target_pool_at_s = None
        self._last_scene_context_revision = None
        self._last_scene_context_at_s = None
        self._last_authorization_challenge_fingerprint = None
        self._last_authorization_challenge_at_s = None

    def close(self) -> None:
        if self._started:
            self.transport.close()
            self._manual_tracker = None
            self._active_selection_command = None
            self._manual_tracker_unavailable = False
            self._started = False

    def _publish(
        self,
        status: TrackStatusMessage,
        peer: OperatorPeer,
        errors: list[str],
    ) -> bool:
        try:
            self.transport.publish_track_status(status, peer=peer)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            errors.append(type(exc).__name__)
            return False
        return True

    def _publish_mission_status(
        self,
        status: MissionStatusMessage,
        peer: OperatorPeer,
        errors: list[str],
    ) -> bool:
        try:
            self.transport.publish_mission_status(status, peer=peer)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            errors.append(type(exc).__name__)
            return False
        return True

    def _publish_safety_status(
        self,
        status: SafetyStatusMessage,
        peer: OperatorPeer,
        errors: list[str],
    ) -> bool:
        try:
            self.transport.publish_safety_status(status, peer=peer)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            errors.append(type(exc).__name__)
            return False
        return True

    def _publish_patrol_status(
        self,
        status: PatrolStatusMessage,
        peer: OperatorPeer,
        errors: list[str],
    ) -> bool:
        publish = getattr(self.transport, "publish_patrol_status", None)
        if not callable(publish):
            return False
        try:
            publish(status, peer=peer)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            errors.append(type(exc).__name__)
            return False
        return True

    def _publish_authorization_challenge(
        self,
        status: AuthorizationChallengeStatusMessage,
        peer: OperatorPeer,
        errors: list[str],
    ) -> bool:
        publish = getattr(self.transport, "publish_authorization_challenge", None)
        if not callable(publish):
            return False
        try:
            publish(status, peer=peer)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            errors.append(type(exc).__name__)
            return False
        return True

    def _publish_range_status(
        self,
        status: RangeStatusMessage,
        peer: OperatorPeer,
        errors: list[str],
    ) -> bool:
        publish = getattr(self.transport, "publish_range_status", None)
        if not callable(publish):
            return False
        try:
            publish(status, peer=peer)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            errors.append(type(exc).__name__)
            return False
        return True

    def _publish_target_geolocation_status(
        self,
        status: TargetGeolocationStatusMessage,
        peer: OperatorPeer,
        errors: list[str],
    ) -> bool:
        publish = getattr(self.transport, "publish_target_geolocation_status", None)
        if not callable(publish):
            return False
        try:
            publish(status, peer=peer)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            errors.append(type(exc).__name__)
            return False
        return True

    def _publish_release_status(
        self,
        status: ReleaseStatusMessage,
        peer: OperatorPeer,
        errors: list[str],
    ) -> bool:
        publish = getattr(self.transport, "publish_release_status", None)
        if not callable(publish):
            return False
        try:
            publish(status, peer=peer)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            errors.append(type(exc).__name__)
            return False
        return True

    def _publish_approach_challenge(
        self,
        status: ApproachChallengeStatusMessage,
        peer: OperatorPeer,
        errors: list[str],
    ) -> bool:
        publish = getattr(self.transport, "publish_approach_challenge", None)
        if not callable(publish):
            return False
        try:
            publish(status, peer=peer)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            errors.append(type(exc).__name__)
            return False
        return True

    def _publish_approach_status(
        self,
        status: ApproachStatusMessage,
        peer: OperatorPeer,
        errors: list[str],
    ) -> bool:
        publish = getattr(self.transport, "publish_approach_status", None)
        if not callable(publish):
            return False
        try:
            publish(status, peer=peer)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            errors.append(type(exc).__name__)
            return False
        return True

    def _publish_payload_target_challenge(
        self,
        status: PayloadTargetChallengeStatusMessage,
        peer: OperatorPeer,
        errors: list[str],
    ) -> bool:
        publish = getattr(self.transport, "publish_payload_target_challenge", None)
        if not callable(publish):
            return False
        try:
            publish(status, peer=peer)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            errors.append(type(exc).__name__)
            return False
        return True

    def _publish_payload_target_status(
        self,
        status: PayloadTargetStatusMessage,
        peer: OperatorPeer,
        errors: list[str],
    ) -> bool:
        publish = getattr(self.transport, "publish_payload_target_status", None)
        if not callable(publish):
            return False
        try:
            publish(status, peer=peer)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            errors.append(type(exc).__name__)
            return False
        return True

    def _publish_target_pool_status(
        self,
        status: TargetPoolStatusMessage,
        peer: OperatorPeer,
        errors: list[str],
    ) -> bool:
        publish = getattr(self.transport, "publish_target_pool_status", None)
        if not callable(publish):
            return False
        try:
            publish(status, peer=peer)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            errors.append(type(exc).__name__)
            return False
        return True

    def _publish_scene_context_status(
        self,
        status: SceneContextStatusMessage,
        peer: OperatorPeer,
        errors: list[str],
    ) -> bool:
        publish = getattr(self.transport, "publish_scene_context_status", None)
        if not callable(publish):
            return False
        try:
            publish(status, peer=peer)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            errors.append(type(exc).__name__)
            return False
        return True

    def _drain_errors(self) -> list[str]:
        errors: list[str] = []
        for _ in range(self.maximum_errors_per_frame):
            error = self.transport.poll_error()
            if error is None:
                break
            errors.append(type(error).__name__)
        return errors

    def _mission_status_due(self, status: MissionStatusMessage) -> bool:
        fingerprint = self._mission_status_fingerprint(status)
        if fingerprint != self._last_mission_status_fingerprint:
            return True
        if self._last_mission_status_at_s is None:
            return True
        return (
            status.produced_at_s - self._last_mission_status_at_s >= self.mission_status_heartbeat_s
        )

    def _safety_status_due(self, status: SafetyStatusMessage) -> bool:
        fingerprint = self._safety_status_fingerprint(status)
        if fingerprint != self._last_safety_status_fingerprint:
            return True
        if self._last_safety_status_at_s is None:
            return True
        return (
            status.produced_at_s - self._last_safety_status_at_s >= self.mission_status_heartbeat_s
        )

    def _authorization_challenge_due(
        self,
        status: AuthorizationChallengeStatusMessage,
    ) -> bool:
        fingerprint = self._authorization_challenge_fingerprint(status)
        if fingerprint != self._last_authorization_challenge_fingerprint:
            return True
        if self._last_authorization_challenge_at_s is None:
            return True
        return (
            status.produced_at_s - self._last_authorization_challenge_at_s
            >= self.mission_status_heartbeat_s
        )

    def _patrol_status_due(self, status: PatrolStatusMessage) -> bool:
        fingerprint = self._patrol_status_fingerprint(status)
        if fingerprint != self._last_patrol_status_fingerprint:
            return True
        if self._last_patrol_status_at_s is None:
            return True
        return (
            status.produced_at_s - self._last_patrol_status_at_s >= self.mission_status_heartbeat_s
        )

    def _range_status_due(self, status: RangeStatusMessage) -> bool:
        fingerprint = self._range_status_fingerprint(status)
        if fingerprint != self._last_range_status_fingerprint:
            return True
        if self._last_range_status_at_s is None:
            return True
        return status.produced_at_s - self._last_range_status_at_s >= self.range_status_heartbeat_s

    def _target_geolocation_status_due(self, status: TargetGeolocationStatusMessage) -> bool:
        fingerprint = self._target_geolocation_status_fingerprint(status)
        if fingerprint != self._last_target_geolocation_status_fingerprint:
            return True
        if self._last_target_geolocation_status_at_s is None:
            return True
        return (
            status.produced_at_s - self._last_target_geolocation_status_at_s
            >= self.target_geolocation_status_heartbeat_s
        )

    def _release_status_due(self, status: ReleaseStatusMessage) -> bool:
        fingerprint = self._release_status_fingerprint(status)
        if fingerprint != self._last_release_status_fingerprint:
            return True
        if self._last_release_status_at_s is None:
            return True
        return (
            status.produced_at_s - self._last_release_status_at_s >= self.release_status_heartbeat_s
        )

    def _approach_challenge_due(self, status: ApproachChallengeStatusMessage) -> bool:
        fingerprint = self._approach_challenge_fingerprint(status)
        if fingerprint != self._last_approach_challenge_fingerprint:
            return True
        if self._last_approach_challenge_at_s is None:
            return True
        return (
            status.produced_at_s - self._last_approach_challenge_at_s
            >= self.mission_status_heartbeat_s
        )

    def _approach_status_due(self, status: ApproachStatusMessage) -> bool:
        fingerprint = self._approach_status_fingerprint(status)
        if fingerprint != self._last_approach_status_fingerprint:
            return True
        if self._last_approach_status_at_s is None:
            return True
        return (
            status.produced_at_s - self._last_approach_status_at_s
            >= self.approach_status_heartbeat_s
        )

    def _payload_target_challenge_due(
        self,
        status: PayloadTargetChallengeStatusMessage,
    ) -> bool:
        fingerprint = self._payload_target_challenge_fingerprint(status)
        if fingerprint != self._last_payload_target_challenge_fingerprint:
            return True
        if self._last_payload_target_challenge_at_s is None:
            return True
        return (
            status.produced_at_s - self._last_payload_target_challenge_at_s
            >= self.mission_status_heartbeat_s
        )

    def _payload_target_status_due(self, status: PayloadTargetStatusMessage) -> bool:
        fingerprint = self._payload_target_status_fingerprint(status)
        if fingerprint != self._last_payload_target_status_fingerprint:
            return True
        if self._last_payload_target_status_at_s is None:
            return True
        return (
            status.produced_at_s - self._last_payload_target_status_at_s
            >= self.payload_target_status_heartbeat_s
        )

    def _target_pool_status_due(
        self,
        statuses: tuple[TargetPoolStatusMessage, ...],
    ) -> bool:
        first = statuses[0]
        if any(
            status.pool_revision != first.pool_revision
            or status.page_count != len(statuses)
            or status.page_index != index
            or status.total_track_count != first.total_track_count
            for index, status in enumerate(statuses)
        ):
            raise ValueError("target-pool status pages are incomplete or inconsistent")
        if self._last_target_pool_revision is None or self._last_target_pool_at_s is None:
            return True
        # Pool revisions can change every camera frame. Publishing every revision
        # churns QGC's Repeater delegates fast enough to cancel pointer releases.
        # Send the freshest complete revision on a bounded cadence instead.
        return (
            first.produced_at_s - self._last_target_pool_at_s >= self.target_pool_status_heartbeat_s
        )

    def _scene_context_status_due(
        self,
        statuses: tuple[SceneContextStatusMessage, ...],
    ) -> bool:
        first = statuses[0]
        if any(
            status.context_revision != first.context_revision
            or status.source_frame_id != first.source_frame_id
            or status.state is not first.state
            or status.page_count != len(statuses)
            or status.page_index != index
            or status.total_region_count != first.total_region_count
            for index, status in enumerate(statuses)
        ):
            raise ValueError("scene-context status pages are incomplete or inconsistent")
        if first.context_revision != self._last_scene_context_revision:
            return True
        if self._last_scene_context_at_s is None:
            return True
        return (
            first.produced_at_s - self._last_scene_context_at_s
            >= self.scene_context_status_heartbeat_s
        )

    @staticmethod
    def _mission_status_fingerprint(status: MissionStatusMessage) -> tuple[object, ...]:
        return (
            status.mission_id,
            status.phase,
            status.authorization_state,
            status.release_window,
            status.safety_allowed,
            status.remaining_payload_count,
            status.total_payload_count,
            status.target_id,
            status.active_payload_slot_id,
            _quantize(status.target_confidence, 254.0),
            _quantize(status.relative_bearing_deg, 100.0),
            _quantize(status.estimated_range_m, 10.0),
            _quantize(status.cross_track_error_m, 10.0),
            _quantize(status.along_track_error_m, 10.0),
            _quantize(status.release_lead_distance_m, 10.0),
        )

    @staticmethod
    def _safety_status_fingerprint(status: SafetyStatusMessage) -> tuple[object, ...]:
        return (
            status.mission_id,
            status.target_id,
            status.ruleset_version,
            tuple((check.rule_id, check.verdict) for check in status.checks),
        )

    @staticmethod
    def _patrol_status_fingerprint(status: PatrolStatusMessage) -> tuple[object, ...]:
        return (
            status.mission_id,
            status.phase,
            status.primary_target_id,
            status.target_state,
            status.bbox.rounded(4) if status.bbox is not None else None,
            status.label,
            _quantize(status.confidence, 254.0),
            _quantize(status.tracking_quality, 254.0),
            status.total_track_count,
            status.locked_track_count,
            status.return_direction,
            status.return_validity,
            _quantize(status.return_evidence_age_s, 10.0),
            _quantize(status.estimated_minimum_turn_radius_m, 10.0),
        )

    @staticmethod
    def _range_status_fingerprint(status: RangeStatusMessage) -> tuple[object, ...]:
        return (
            status.target_id,
            status.calibration_id,
            status.validity,
            status.reasons,
            status.sources,
            status.rejected_sources,
            _quantize(status.slant_range_m, 10.0),
            _quantize(status.ground_range_m, 10.0),
            tuple(_quantize(value, 10.0) for value in status.slant_range_ci95_m)
            if status.slant_range_ci95_m is not None
            else None,
            tuple(_quantize(value, 10.0) for value in status.ground_range_ci95_m)
            if status.ground_range_ci95_m is not None
            else None,
            _quantize(status.relative_bearing_deg, 100.0),
            _quantize(status.absolute_bearing_deg, 100.0),
            _quantize(status.bearing_sigma_deg, 100.0),
            _quantize(status.north_offset_m, 10.0),
            _quantize(status.east_offset_m, 10.0),
            _quantize(status.data_freshness_s, 10.0),
            _quantize(status.sensor_consistency, 254.0),
        )

    @staticmethod
    def _target_geolocation_status_fingerprint(
        status: TargetGeolocationStatusMessage,
    ) -> tuple[object, ...]:
        return (
            status.target_id,
            status.source_frame_id,
            status.available,
            status.reason,
            _quantize(status.latitude_deg, 10_000_000.0),
            _quantize(status.longitude_deg, 10_000_000.0),
            _quantize(status.horizontal_sigma_m, 10.0),
        )

    @staticmethod
    def _release_status_fingerprint(status: ReleaseStatusMessage) -> tuple[object, ...]:
        return (
            status.target_id,
            status.calibration_id,
            status.timing_status,
            status.reasons,
            status.range_target_id,
            status.range_frame_id,
            _quantize(status.target_north_offset_m, 10.0),
            _quantize(status.target_east_offset_m, 10.0),
            _quantize(status.impact_north_offset_m, 10.0),
            _quantize(status.impact_east_offset_m, 10.0),
            _quantize(status.along_track_error_m, 10.0),
            _quantize(status.cross_track_error_m, 10.0),
            _quantize(status.error_ellipse_major_m, 10.0),
            _quantize(status.error_ellipse_minor_m, 10.0),
            _quantize(status.error_ellipse_orientation_deg, 100.0),
            _quantize(status.estimated_ground_range_m, 10.0),
            tuple(_quantize(value, 10.0) for value in status.ground_range_ci95_m)
            if status.ground_range_ci95_m is not None
            else None,
            _quantize(status.payload_descent_time_s, 10.0),
            _quantize(status.release_lead_distance_m, 10.0),
            _quantize(status.range_sensor_consistency, 254.0),
        )

    @staticmethod
    def _approach_challenge_fingerprint(
        status: ApproachChallengeStatusMessage,
    ) -> tuple[object, ...]:
        return (
            status.challenge_token,
            status.target_token,
            status.target_revision,
            status.selection_command_id,
            _quantize(status.expires_at_s, 1000.0),
        )

    @staticmethod
    def _approach_status_fingerprint(status: ApproachStatusMessage) -> tuple[object, ...]:
        return (
            status.target_id,
            status.target_revision,
            status.phase,
            status.reasons,
            _quantize(status.yaw_error_deg, 100.0),
            _quantize(status.pitch_error_deg, 100.0),
            _quantize(status.yaw_advice_deg, 100.0),
            _quantize(status.pitch_advice_deg, 100.0),
            _quantize(status.bank_advice_deg, 100.0),
            _quantize(status.climb_pitch_advice_deg, 100.0),
            _quantize(status.ground_range_m, 10.0),
            _quantize(status.confirmation_expires_at_s, 10.0),
        )

    @staticmethod
    def _payload_target_challenge_fingerprint(
        status: PayloadTargetChallengeStatusMessage,
    ) -> tuple[object, ...]:
        return (
            status.challenge_token,
            status.selected_target_token,
            status.selected_target_revision,
            status.aimpoint_target_token,
            status.aimpoint_target_revision,
            status.selection_command_id,
            _quantize(status.expires_at_s, 1000.0),
        )

    @staticmethod
    def _payload_target_status_fingerprint(
        status: PayloadTargetStatusMessage,
    ) -> tuple[object, ...]:
        return (
            status.selection_command_id,
            status.selected_target_token,
            status.selected_target_revision,
            status.eligibility,
            status.aimpoint_target_token,
            status.aimpoint_target_revision,
            status.confirmation_pending,
            status.confirmation_accepted,
            _quantize(status.confirmation_expires_at_s, 10.0),
        )

    @staticmethod
    def _authorization_challenge_fingerprint(
        status: AuthorizationChallengeStatusMessage,
    ) -> tuple[object, ...]:
        return (
            status.challenge_token,
            status.mission_token,
            status.target_token,
            status.scene_token,
            status.ruleset_token,
            status.payload_slot_token,
            status.target_revision,
            status.created_at_s,
            status.expires_at_s,
            status.pending,
        )


def _quantize(value: float | None, scale: float) -> int | None:
    return None if value is None else round(value * scale)


__all__ = [
    "LiveOperatorBridge",
    "OperatorBridgeResult",
    "OperatorPeer",
    "OperatorStatusTransport",
]
