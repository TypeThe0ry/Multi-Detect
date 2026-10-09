"""Truth-free realtime perception bridge for the shared OnboardAI path.

The detector/tracker and vehicle telemetry adapters in SIM and Jetson produce
different wire formats, but they must enter the same high-level decision path.
This module is that narrow boundary.  It accepts a validated structured
perception observation, converts it to :class:`OnboardRuntimeEvent`, and
serializes it through :class:`OnboardMissionEventLoop`.

The bridge deliberately does not know about MAVLink, Gazebo truth, camera
pixels, PWM, arming, or a transport socket.  A platform adapter owns those
concerns and can install the canonical Mission through the callbacks already
provided by ``OnboardMissionAdapter``.  The exact same bridge can therefore be
used by a Gazebo sensor callback and by the Jetson camera process.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from .contracts import ContextSnapshot, ExecutableMission, GeoPoint
from .onboard_adapter import OnboardMissionEventLoop, OnboardMissionRevision
from .onboard_agent import (
    OnboardEventKind,
    OnboardRuntimeEvent,
    OnboardTaskAction,
)
from .onboard_task_state import OnboardTaskReceipt
from .serialization import (
    canonical_sha256,
    optional_real,
    require_bool,
    require_real,
    require_text,
)
from .task_selection import TaskCandidate


@dataclass(frozen=True, slots=True)
class OnboardPerceptionObservation:
    """One detector/tracker result with no simulator truth attached.

    ``target_class_match`` and ``target_color_match`` are supplied by the
    detector/tracker/task matcher.  They are not inferred from Gazebo truth.
    Unknown values remain ``None`` and are encoded by the shared r4 feature
    contract as ``0.5``.
    """

    observation_id: str
    observed_at_s: float
    source: str
    target_visible: bool
    target_class: str | None = None
    target_color: str | None = None
    target_class_match: bool | None = None
    target_color_match: bool | None = None
    target_bearing_deg: float | None = None
    target_geometry_frame: str = "ned"
    target_distance_m: float | None = None
    target_north_offset_m: float | None = None
    target_east_offset_m: float | None = None
    target_confidence: float | None = None
    target_match_score: float | None = None
    obstacle_bearing_deg: float | None = None
    obstacle_distance_m: float | None = None
    route_progress_fraction: float | None = None
    task_phase: OnboardTaskAction | None = None
    detail: str = ""
    severity: int = 0
    source_frame_sha256: str | None = None
    truth_free: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "observation_id", require_text(self.observation_id, "observation_id")
        )
        object.__setattr__(self, "source", require_text(self.source, "source"))
        object.__setattr__(
            self, "observed_at_s", require_real(self.observed_at_s, "observed_at_s", minimum=0.0)
        )
        require_bool(self.target_visible, "target_visible")
        require_bool(self.truth_free, "truth_free")
        if not self.truth_free:
            raise ValueError("perception observation must be truth_free")
        if isinstance(self.severity, bool) or not isinstance(self.severity, int):
            raise TypeError("severity must be an integer")
        if not 0 <= self.severity <= 100:
            raise ValueError("severity must be in 0..100")
        for name in ("target_class", "target_color"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, require_text(value, name).lower())
        for name in ("target_class_match", "target_color_match"):
            value = getattr(self, name)
            if value is not None:
                require_bool(value, name)
        for name, minimum, maximum in (
            ("target_bearing_deg", -180.0, 180.0),
            ("target_distance_m", 0.0, 5_000.0),
            ("target_north_offset_m", -5_000.0, 5_000.0),
            ("target_east_offset_m", -5_000.0, 5_000.0),
            ("target_confidence", 0.0, 1.0),
            ("target_match_score", 0.0, 1.0),
            ("obstacle_bearing_deg", -180.0, 180.0),
            ("obstacle_distance_m", 0.0, 5_000.0),
            ("route_progress_fraction", 0.0, 1.0),
        ):
            object.__setattr__(
                self,
                name,
                optional_real(getattr(self, name), name, minimum=minimum, maximum=maximum),
            )
        if self.target_geometry_frame not in {"ned", "camera_relative"}:
            raise ValueError("target_geometry_frame must be 'ned' or 'camera_relative'")
        if self.target_geometry_frame == "camera_relative" and (
            self.target_north_offset_m is not None or self.target_east_offset_m is not None
        ):
            raise ValueError("camera_relative geometry cannot carry calibrated NED offsets")
        if self.task_phase is not None and not isinstance(self.task_phase, OnboardTaskAction):
            raise TypeError("task_phase must be an OnboardTaskAction or None")
        detail = self.detail.strip() if isinstance(self.detail, str) else self.detail
        object.__setattr__(
            self,
            "detail",
            detail or ("target_visible" if self.target_visible else "target_lost"),
        )
        if self.source_frame_sha256 is not None:
            raw = require_text(self.source_frame_sha256, "source_frame_sha256").lower()
            if len(raw) != 64 or any(char not in "0123456789abcdef" for char in raw):
                raise ValueError("source_frame_sha256 must be a lowercase SHA-256")
            object.__setattr__(self, "source_frame_sha256", raw)

    @property
    def event_kind(self) -> OnboardEventKind:
        return (
            OnboardEventKind.TARGET_MOVED if self.target_visible else OnboardEventKind.TARGET_LOST
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "contract": "multidetect.onboard-perception-observation",
            "observation_id": self.observation_id,
            "observed_at_s": self.observed_at_s,
            "source": self.source,
            "target_visible": self.target_visible,
            "target_class": self.target_class,
            "target_color": self.target_color,
            "target_class_match": self.target_class_match,
            "target_color_match": self.target_color_match,
            "target_bearing_deg": self.target_bearing_deg,
            "target_geometry_frame": self.target_geometry_frame,
            "target_distance_m": self.target_distance_m,
            "target_north_offset_m": self.target_north_offset_m,
            "target_east_offset_m": self.target_east_offset_m,
            "target_confidence": self.target_confidence,
            "target_match_score": self.target_match_score,
            "obstacle_bearing_deg": self.obstacle_bearing_deg,
            "obstacle_distance_m": self.obstacle_distance_m,
            "route_progress_fraction": self.route_progress_fraction,
            "task_phase": None if self.task_phase is None else self.task_phase.value,
            "detail": self.detail,
            "severity": self.severity,
            "source_frame_sha256": self.source_frame_sha256,
            "truth_free": True,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    def to_runtime_event(self) -> OnboardRuntimeEvent:
        return OnboardRuntimeEvent(
            event_id=self.observation_id,
            kind=self.event_kind,
            observed_at_s=self.observed_at_s,
            severity=self.severity,
            detail=self.detail,
            target_bearing_deg=self.target_bearing_deg,
            target_geometry_frame=self.target_geometry_frame,
            target_distance_m=self.target_distance_m,
            target_north_offset_m=self.target_north_offset_m,
            target_east_offset_m=self.target_east_offset_m,
            target_confidence=self.target_confidence,
            target_match_score=self.target_match_score,
            target_class=self.target_class,
            target_color=self.target_color,
            target_class_match=self.target_class_match,
            target_color_match=self.target_color_match,
            obstacle_bearing_deg=self.obstacle_bearing_deg,
            obstacle_distance_m=self.obstacle_distance_m,
            route_progress_fraction=self.route_progress_fraction,
            task_phase=self.task_phase,
        )


class OnboardRealtimeModelBridge:
    """Feed structured live observations into the shared OnboardAI session."""

    __slots__ = (
        "event_loop",
        "_candidates",
        "_runtimes",
        "_prepared",
        "_observations",
        "_observation_hashes",
        "_revisions",
        "_context_hashes",
    )

    def __init__(
        self,
        event_loop: OnboardMissionEventLoop,
        *,
        candidates: Sequence[TaskCandidate],
        runtimes: Mapping[str, object],
    ) -> None:
        if not isinstance(event_loop, OnboardMissionEventLoop):
            raise TypeError("event_loop must be an OnboardMissionEventLoop")
        if isinstance(candidates, (str, bytes, bytearray)) or not isinstance(candidates, Sequence):
            raise TypeError("candidates must be a sequence")
        entries = tuple(candidates)
        if not entries or any(not isinstance(item, TaskCandidate) for item in entries):
            raise ValueError("candidates must be a non-empty TaskCandidate sequence")
        if not isinstance(runtimes, Mapping):
            raise TypeError("runtimes must be a mapping")
        ids = tuple(item.task.task_id for item in entries)
        if len(ids) != len(set(ids)) or any(task_id not in runtimes for task_id in ids):
            raise ValueError("candidate task IDs and runtimes must match")
        self.event_loop = event_loop
        self._candidates = entries
        self._runtimes = dict(runtimes)
        self._prepared = False
        self._observations: list[OnboardPerceptionObservation] = []
        self._observation_hashes: dict[str, str] = {}
        self._revisions: list[OnboardMissionRevision] = []
        self._context_hashes: list[str] = []

    @property
    def prepared(self) -> bool:
        return self._prepared

    @property
    def observations(self) -> tuple[OnboardPerceptionObservation, ...]:
        return tuple(self._observations)

    @property
    def revisions(self) -> tuple[OnboardMissionRevision, ...]:
        return tuple(self._revisions)

    def prepare(
        self, context: ContextSnapshot, *, mission_id: str | None = None
    ) -> OnboardMissionRevision:
        if self._prepared:
            raise RuntimeError("realtime model bridge is already prepared")
        revision = self.event_loop.prepare(
            context,
            candidates=self._candidates,
            runtimes=self._runtimes,
            mission_id=mission_id,
        )
        self._prepared = True
        self._context_hashes.append(context.canonical_hash)
        self._revisions.append(revision)
        return revision

    def submit(
        self,
        context: ContextSnapshot,
        observation: OnboardPerceptionObservation,
        *,
        mission_id: str | None = None,
    ) -> OnboardMissionRevision:
        if not self._prepared:
            raise RuntimeError("prepare must be called before submit")
        if not isinstance(observation, OnboardPerceptionObservation):
            raise TypeError("observation must be an OnboardPerceptionObservation")
        revision = self.submit_runtime_event(
            context,
            observation.to_runtime_event(),
            mission_id=mission_id,
        )
        if observation.observation_id not in self._observation_hashes:
            self._observations.append(observation)
            self._observation_hashes[observation.observation_id] = observation.canonical_hash
        self._context_hashes.append(context.canonical_hash)
        if not self._revisions or revision.sequence > self._revisions[-1].sequence:
            self._revisions.append(revision)
        return revision

    def submit_runtime_event(
        self,
        context: ContextSnapshot,
        event: OnboardRuntimeEvent,
        *,
        mission_id: str | None = None,
    ) -> OnboardMissionRevision:
        """Submit a truth-free vehicle/runtime event through the same bridge.

        Perception frames use :meth:`submit`, while a transport adapter uses
        this method for events proven by telemetry (for example a return leg
        reaching route progress 1.0).  Both paths share the exact executor,
        planner revalidation, Mission transaction callback and idempotency
        guards; telemetry cannot bypass the onboard state machine.
        """

        if not self._prepared:
            raise RuntimeError("prepare must be called before submit_runtime_event")
        if not isinstance(event, OnboardRuntimeEvent):
            raise TypeError("event must be an OnboardRuntimeEvent")
        revision = self.event_loop.submit(
            context,
            event,
            candidates=self._candidates,
            runtimes=self._runtimes,
            mission_id=mission_id,
        )
        self._context_hashes.append(context.canonical_hash)
        if not self._revisions or revision.sequence > self._revisions[-1].sequence:
            self._revisions.append(revision)
        return revision

    def mark_takeoff_complete(
        self,
        context: ContextSnapshot,
        *,
        minimum_relative_altitude_m: float = 10.0,
    ) -> bool:
        """Advance the shared task lifecycle from qualified live telemetry."""

        if not self._prepared:
            raise RuntimeError("prepare must be called before marking takeoff complete")
        changed = self.event_loop.mark_takeoff_complete(
            context,
            minimum_relative_altitude_m=minimum_relative_altitude_m,
        )
        if changed:
            self._context_hashes.append(context.canonical_hash)
        return changed

    def evidence(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "contract": "multidetect.onboard-realtime-model-bridge",
            "prepared": self._prepared,
            "processed_observation_count": len(self._observations),
            "context_sha256": list(self._context_hashes),
            "observations": [item.to_mapping() for item in self._observations],
            "revisions": [item.to_mapping() for item in self._revisions],
            "event_loop": self.event_loop.evidence(),
            "policy_applied_to_actuator": False,
            "truth_free": True,
        }


MissionStartDispatcher = Callable[
    [ContextSnapshot, ExecutableMission, OnboardMissionRevision], None
]
ReturnRequestDispatcher = Callable[[ContextSnapshot, OnboardMissionRevision], None]
TaskReportDispatcher = Callable[[OnboardTaskReceipt], None]


@dataclass(frozen=True, slots=True)
class OnboardAutonomousSortieStatus:
    """Observable status for one prompt-driven sortie.

    This is deliberately a status document, not a command object.  The
    injected dispatchers are the only place that may arm, start, replace or
    command a vehicle.  A Gazebo adapter and the Jetson adapter therefore use
    the same state machine while retaining their own transport lifecycle.
    """

    phase: OnboardTaskAction
    takeoff_confirmed: bool
    mission_started: bool
    return_requested: bool
    report_committed: bool
    target_position: GeoPoint | None
    target_receipt_sha256: str | None
    last_mission_id: str | None

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "contract": "multidetect.onboard-autonomous-sortie-status",
            "phase": self.phase.value,
            "takeoff_confirmed": self.takeoff_confirmed,
            "mission_started": self.mission_started,
            "return_requested": self.return_requested,
            "report_committed": self.report_committed,
            "target_position": (
                None if self.target_position is None else self.target_position.to_mapping()
            ),
            "target_receipt_sha256": self.target_receipt_sha256,
            "last_mission_id": self.last_mission_id,
            "policy_applied_to_actuator": False,
            "truth_free": True,
        }


# A model search proposal is a bounded local Mission, not a per-frame velocity
# command. Keep that Mission alive until the vehicle has travelled most of the
# proposal's search leg; otherwise a slow SITL/Gazebo stream can replace the
# same route every few metres and never reach the target corridor.
_SEARCH_REPLAN_DISTANCE_M = 50.0


class OnboardAutonomousSortie:
    """Run a complete prompt-to-report task through the shared realtime path.

    The class is the missing orchestration layer between the realtime bridge
    and a platform lifecycle adapter.  It does not invent a second planner,
    detector or flight controller.  Every frame and telemetry event enters
    ``OnboardRealtimeModelBridge``; each model proposal is still checked by
    ``OnboardMissionAdapter`` before a dispatcher is called.

    A normal red-car sortie is therefore:

    ``prepare/start -> qualified takeoff -> green reject/search -> red match ->
    record GPS -> return completion -> report``.

    ``mission_start_dispatcher`` is where a SIM or PX4 adapter performs its
    existing upload/readback/commit/permit plus arm/start lifecycle.  The
    realtime model never receives that callback.
    """

    __slots__ = (
        "bridge",
        "mission_start_dispatcher",
        "return_request_dispatcher",
        "report_dispatcher",
        "_prepared",
        "_takeoff_confirmed",
        "_mission_started",
        "_return_requested",
        "_report_committed",
        "_target_position",
        "_target_receipt_sha256",
        "_last_mission_id",
        "_last_context",
        "_last_target_observation",
        "_last_search_signature",
        "_last_search_position",
        "_suppressed_search_observations",
    )

    def __init__(
        self,
        bridge: OnboardRealtimeModelBridge,
        *,
        mission_start_dispatcher: MissionStartDispatcher | None = None,
        return_request_dispatcher: ReturnRequestDispatcher | None = None,
        report_dispatcher: TaskReportDispatcher | None = None,
    ) -> None:
        if not isinstance(bridge, OnboardRealtimeModelBridge):
            raise TypeError("bridge must be an OnboardRealtimeModelBridge")
        for callback, name in (
            (mission_start_dispatcher, "mission_start_dispatcher"),
            (return_request_dispatcher, "return_request_dispatcher"),
            (report_dispatcher, "report_dispatcher"),
        ):
            if callback is not None and not callable(callback):
                raise TypeError(f"{name} must be callable")
        self.bridge = bridge
        self.mission_start_dispatcher = mission_start_dispatcher
        self.return_request_dispatcher = return_request_dispatcher
        self.report_dispatcher = report_dispatcher
        self._prepared = False
        self._takeoff_confirmed = False
        self._mission_started = False
        self._return_requested = False
        self._report_committed = False
        self._target_position: GeoPoint | None = None
        self._target_receipt_sha256: str | None = None
        self._last_mission_id: str | None = None
        self._last_context: ContextSnapshot | None = None
        self._last_target_observation: OnboardPerceptionObservation | None = None
        # A camera can produce dozens of identical distractor frames while a
        # search Mission is being uploaded or executed.  Re-uploading the
        # same bounded move on every frame creates a PX4 Mission replacement
        # race and does not add information.  Coarse spatial bins keep the
        # gate truth-free while allowing a materially changed view after the
        # vehicle moves to generate a fresh search decision.
        self._last_search_signature: tuple[object, ...] | None = None
        self._last_search_position: tuple[float, float] | None = None
        self._suppressed_search_observations = 0

    @property
    def prepared(self) -> bool:
        return self._prepared

    @property
    def status(self) -> OnboardAutonomousSortieStatus:
        return OnboardAutonomousSortieStatus(
            phase=self.bridge.event_loop.adapter.task_state.phase,
            takeoff_confirmed=self._takeoff_confirmed,
            mission_started=self._mission_started,
            return_requested=self._return_requested,
            report_committed=self._report_committed,
            target_position=self._target_position,
            target_receipt_sha256=self._target_receipt_sha256,
            last_mission_id=self._last_mission_id,
        )

    def _dispatch_revision(
        self, context: ContextSnapshot, revision: OnboardMissionRevision
    ) -> None:
        if revision.mission is not None:
            self._last_mission_id = revision.mission.mission_id
            if self.mission_start_dispatcher is not None:
                self.mission_start_dispatcher(context, revision.mission, revision)
                self._mission_started = True
        if (
            revision.decision.response.value == "return"
            and not self._return_requested
            and self.return_request_dispatcher is not None
        ):
            self.return_request_dispatcher(context, revision)
            self._return_requested = True
        receipt = revision.task_receipt
        if receipt is not None:
            if receipt.target_position is not None:
                self._target_position = receipt.target_position
                self._target_receipt_sha256 = receipt.event_sha256
            if receipt.phase is OnboardTaskAction.REPORT:
                if self.report_dispatcher is not None:
                    self.report_dispatcher(receipt)
                self._report_committed = True

    def prepare(
        self,
        context: ContextSnapshot,
        *,
        mission_id: str | None = None,
    ) -> OnboardMissionRevision:
        """Prepare the prompt and start the first compiled Mission once."""

        if self._prepared:
            raise RuntimeError("autonomous sortie is already prepared")
        revision = self.bridge.prepare(context, mission_id=mission_id)
        self._prepared = True
        self._last_context = context
        self._dispatch_revision(context, revision)
        return revision

    def ingest_telemetry(
        self,
        context: ContextSnapshot,
        *,
        minimum_relative_altitude_m: float = 10.0,
    ) -> bool:
        """Feed qualified live telemetry and close the shared takeoff gate."""

        if not self._prepared:
            raise RuntimeError("prepare must be called before ingest_telemetry")
        self._last_context = context
        if self.status.phase is not OnboardTaskAction.TAKEOFF:
            return False
        changed = self.bridge.mark_takeoff_complete(
            context,
            minimum_relative_altitude_m=minimum_relative_altitude_m,
        )
        self._takeoff_confirmed = self._takeoff_confirmed or changed
        return changed

    def ingest_perception(
        self,
        context: ContextSnapshot,
        observation: OnboardPerceptionObservation,
        *,
        mission_id: str | None = None,
    ) -> OnboardMissionRevision:
        """Run one detector/tracker observation without an operator click."""

        if not self._prepared:
            raise RuntimeError("prepare must be called before ingest_perception")
        self._last_context = context
        if self.status.phase is OnboardTaskAction.TAKEOFF:
            raise RuntimeError("qualified takeoff telemetry is required before perception")
        search_signature = self._search_signature(observation)
        if search_signature is not None:
            if self._last_search_signature is not None and not self._search_position_changed(
                context
            ):
                # Mission deduplication must not erase occlusion/dropout time
                # from a scoped history model. This hook only samples inputs;
                # it does not infer, upload a Mission or change task receipts.
                self.bridge.event_loop.observe_model_input(context, observation.to_runtime_event())
                self._suppressed_search_observations += 1
                if not self.bridge.revisions:
                    raise RuntimeError("cannot suppress a search frame before the first revision")
                return self.bridge.revisions[-1]
            self._last_search_signature = search_signature
            self._last_search_position = (
                context.aircraft_position.latitude_deg,
                context.aircraft_position.longitude_deg,
            )
        else:
            # A matched colour or a non-search phase opens a new search epoch.
            # A transient target loss stays in the current search epoch: the
            # already-uploaded Mission must not be replaced merely because a
            # detector frame arrived without a box while PX4 is moving.
            self._last_search_signature = None
            self._last_search_position = None
        if observation.target_visible:
            self._last_target_observation = observation
        revision = self.bridge.submit(context, observation, mission_id=mission_id)
        self._dispatch_revision(context, revision)
        return revision

    @staticmethod
    def _search_signature(
        observation: OnboardPerceptionObservation,
    ) -> tuple[object, ...] | None:
        """Return a coarse identity for an unchanged search view.

        A no-detection SEARCH frame gets a sentinel identity as well.  It is
        still an observation in the same search epoch and must not reset the
        Mission on every temporary detector dropout.
        """

        if (
            observation.target_class_match is True and observation.target_color_match is not False
        ) or observation.task_phase not in {None, OnboardTaskAction.SEARCH}:
            return None
        bearing = (
            None
            if observation.target_bearing_deg is None
            else round(observation.target_bearing_deg / 5.0) * 5.0
        )
        distance = (
            None
            if observation.target_distance_m is None
            else round(observation.target_distance_m / 5.0) * 5.0
        )
        return (
            observation.target_class if observation.target_visible else "__no_target__",
            observation.target_color,
            observation.target_class_match,
            observation.target_color_match,
            bearing,
            distance,
        )

    def _search_position_changed(self, context: ContextSnapshot) -> bool:
        """Allow a new search proposal only after meaningful vehicle motion."""

        if self._last_search_position is None:
            return True
        last_latitude, last_longitude = self._last_search_position
        north_m = (context.aircraft_position.latitude_deg - last_latitude) * 111_320.0
        east_m = (
            (context.aircraft_position.longitude_deg - last_longitude)
            * 111_320.0
            * math.cos(math.radians(last_latitude))
        )
        return math.hypot(north_m, east_m) >= _SEARCH_REPLAN_DISTANCE_M

    def record_target_location(
        self,
        context: ContextSnapshot,
        observation: OnboardPerceptionObservation,
        *,
        mission_id: str | None = None,
    ) -> OnboardMissionRevision:
        """Submit the stable, range-qualified target frame for GPS recording.

        This method is called by the realtime detector/ranger after the same
        target has remained confirmed and a valid range/GPS solution exists.
        It is an internal perception step, not an operator confirmation.
        """

        if self.status.phase is not OnboardTaskAction.MATCH_TARGET:
            raise RuntimeError("target location recording requires MATCH_TARGET phase")
        if not observation.target_visible:
            raise RuntimeError("target location recording requires a visible target")
        return self.ingest_perception(context, observation, mission_id=mission_id)

    def confirm_and_record_target_location(
        self,
        context: ContextSnapshot,
        observation: OnboardPerceptionObservation,
        *,
        mission_id_prefix: str | None = None,
        record_context: ContextSnapshot | None = None,
        record_observation: OnboardPerceptionObservation | None = None,
    ) -> tuple[OnboardMissionRevision, OnboardMissionRevision]:
        """Run the model's match and record phases for one qualified frame.

        Temporal models require a second real, newer detector/context sample
        for recording; no duplicate frame or synthetic epsilon time is added
        to history. Missing/stale second inputs are rejected before matching.
        Callers may instead match now and record on the next ingestion tick.
        Legacy single-frame providers retain the same-frame convenience path.
        No phase is written directly and both decisions still pass the normal
        state, planner, safety and Mission transaction gates.
        """

        if not isinstance(observation, OnboardPerceptionObservation):
            raise TypeError("observation must be an OnboardPerceptionObservation")
        if not observation.target_visible:
            raise RuntimeError("target confirmation requires a visible target")
        if observation.target_north_offset_m is None or observation.target_east_offset_m is None:
            raise RuntimeError("target confirmation requires calibrated NED offsets")
        if (record_context is None) != (record_observation is None):
            raise ValueError("record_context and record_observation must be supplied together")
        if record_context is None:
            if self._requires_fresh_phase_observation():
                raise RuntimeError("temporal recording requires a second real observation")
            recording_context = context
            recording_observation = observation
        else:
            if not isinstance(record_observation, OnboardPerceptionObservation):
                raise TypeError("record_observation must be an OnboardPerceptionObservation")
            self._require_fresh_phase_context(
                context,
                record_context,
                previous_observed_at_s=observation.observed_at_s,
                observed_at_s=record_observation.observed_at_s,
            )
            if record_observation.observation_id == observation.observation_id:
                raise ValueError("recording requires a distinct observation ID")
            if (
                not record_observation.target_visible
                or record_observation.target_geometry_frame != "ned"
                or record_observation.target_north_offset_m is None
                or record_observation.target_east_offset_m is None
            ):
                raise ValueError("recording requires a fresh visible, calibrated NED target")
            recording_context = record_context
            recording_observation = record_observation
        prefix = mission_id_prefix or observation.observation_id
        match_observation = replace(
            observation,
            observation_id=f"{prefix}:match",
            task_phase=(
                OnboardTaskAction.SEARCH
                if observation.task_phase is None
                else observation.task_phase
            ),
            detail=f"{observation.detail}; model match confirmation",
        )
        match_revision = self.ingest_perception(
            context,
            match_observation,
            mission_id=f"{prefix}:match",
        )
        if self.status.phase is not OnboardTaskAction.MATCH_TARGET:
            raise RuntimeError(
                "model did not confirm target before location recording; "
                f"phase={self.status.phase.value}"
            )
        record_observation = replace(
            recording_observation,
            observation_id=(
                f"{prefix}:record"
                if record_context is None
                else f"{recording_observation.observation_id}:record"
            ),
            task_phase=OnboardTaskAction.RECORD_LOCATION,
            detail=f"{recording_observation.detail}; model location record confirmation",
        )
        record_revision = self.record_target_location(
            recording_context,
            record_observation,
            mission_id=f"{prefix}:record",
        )
        return match_revision, record_revision

    def complete_return(
        self,
        context: ContextSnapshot,
        *,
        event_id: str = "return-home-complete",
        mission_id: str | None = None,
    ) -> OnboardMissionRevision:
        """Submit telemetry-proven home arrival to the same model path."""

        if not self._prepared:
            raise RuntimeError("prepare must be called before complete_return")
        if self.status.phase is not OnboardTaskAction.RECORD_LOCATION:
            raise RuntimeError("return completion requires a recorded target location")
        target = self._last_target_observation
        event = OnboardRuntimeEvent(
            event_id=event_id,
            kind=OnboardEventKind.MISSION_COMPLETE,
            observed_at_s=context.captured_at_s,
            target_confidence=None if target is None else target.target_confidence,
            target_match_score=None if target is None else target.target_match_score,
            route_progress_fraction=1.0,
            obstacle_distance_m=5_000.0,
            # The telemetry event closes the outgoing RECORD_LOCATION leg;
            # the model must observe that current phase before proposing the
            # RETURN_HOME receipt.  The next report event uses RETURN_HOME.
            task_phase=OnboardTaskAction.RECORD_LOCATION,
            detail="PX4 telemetry confirmed return to home",
        )
        revision = self.bridge.submit_runtime_event(context, event, mission_id=mission_id)
        self._last_context = context
        self._dispatch_revision(context, revision)
        return revision

    def report_after_return(
        self,
        context: ContextSnapshot,
        *,
        event_id: str = "task-report-complete",
    ) -> OnboardMissionRevision:
        """Let the model close the task and emit the report receipt."""

        if not self._prepared:
            raise RuntimeError("prepare must be called before report_after_return")
        if self.status.phase is not OnboardTaskAction.RETURN_HOME:
            raise RuntimeError(
                f"report requires confirmed return-home phase; current={self.status.phase.value}"
            )
        event = OnboardRuntimeEvent(
            event_id=event_id,
            kind=OnboardEventKind.MISSION_COMPLETE,
            observed_at_s=context.captured_at_s,
            route_progress_fraction=1.0,
            obstacle_distance_m=5_000.0,
            task_phase=OnboardTaskAction.RETURN_HOME,
            detail="return completed; report task",
        )
        revision = self.bridge.submit_runtime_event(context, event)
        self._last_context = context
        self._dispatch_revision(context, revision)
        return revision

    def complete_return_and_report(
        self,
        context: ContextSnapshot,
        *,
        return_event_id: str = "return-home-complete",
        report_event_id: str = "task-report-complete",
        report_context: ContextSnapshot | None = None,
    ) -> tuple[OnboardMissionRevision, OnboardMissionRevision]:
        """Close return/report with distinct real telemetry ticks for temporal models."""

        if return_event_id == report_event_id:
            raise ValueError("return and report require distinct event IDs")
        if report_context is None:
            if self._requires_fresh_phase_observation():
                raise RuntimeError("temporal reporting requires a second real context")
            reporting_context = context
        else:
            self._require_fresh_phase_context(
                context,
                report_context,
                previous_observed_at_s=context.captured_at_s,
                observed_at_s=report_context.captured_at_s,
            )
            reporting_context = report_context

        returned = self.complete_return(context, event_id=return_event_id)
        reported = self.report_after_return(reporting_context, event_id=report_event_id)
        return returned, reported

    def _requires_fresh_phase_observation(self) -> bool:
        provider = self.bridge.event_loop.adapter.executor.decision_provider
        required = getattr(provider, "requires_fresh_phase_observation", False)
        if type(required) is not bool:
            raise TypeError("requires_fresh_phase_observation must be bool")
        return required

    def _require_fresh_phase_context(
        self,
        previous: ContextSnapshot,
        current: ContextSnapshot,
        *,
        previous_observed_at_s: float,
        observed_at_s: float,
    ) -> None:
        if not isinstance(previous, ContextSnapshot) or not isinstance(current, ContextSnapshot):
            raise TypeError("phase observations require ContextSnapshot values")
        if (
            current.captured_at_s <= previous.captured_at_s
            or observed_at_s <= previous_observed_at_s
            or observed_at_s < previous.captured_at_s
            or observed_at_s > current.captured_at_s
            or current.snapshot_id == previous.snapshot_id
        ):
            raise ValueError("phase progression requires a strictly newer real observation/context")
        if (
            current.airframe_profile_id != previous.airframe_profile_id
            or current.airframe_profile_hash != previous.airframe_profile_hash
            or current.home_position != previous.home_position
        ):
            raise ValueError("phase progression cannot change airframe or home identity")
        if self._requires_fresh_phase_observation():
            provider = self.bridge.event_loop.adapter.executor.decision_provider
            gap = require_real(
                getattr(provider, "maximum_phase_observation_gap_s", None),
                "history phase observation max_gap_s",
                minimum=0,
            )
            if gap <= 0 or observed_at_s - previous_observed_at_s > gap:
                raise ValueError("phase observations exceed the temporal history gap limit")


__all__ = [
    "MissionStartDispatcher",
    "OnboardAutonomousSortie",
    "OnboardAutonomousSortieStatus",
    "OnboardPerceptionObservation",
    "OnboardRealtimeModelBridge",
    "ReturnRequestDispatcher",
    "TaskReportDispatcher",
]
