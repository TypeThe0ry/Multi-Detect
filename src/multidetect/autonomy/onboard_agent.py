"""Shared real-time OnboardAI orchestration above the PX4 actuator boundary.

The prompt is an immutable mission request, not a frame-by-frame flight
controller.  A caller supplies the current truth-free perception/context and
the resolved task candidates; this module runs the existing shared policy and
planner/compiler, then returns a hash-bound proposal for the existing SIM,
HIL or REAL Mission adapter.  Safety events never manufacture a mission and
never emit PWM, arm/disarm or transport commands.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from time import perf_counter
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

from ..compat import StrEnum
from ..mavlink_gps import (
    GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M,
    MAVLINK_QUALIFIED_FIX_TYPE_MIN,
)
from .contracts import ContextSnapshot, PromptEnvelope
from .mission_campaign import CampaignTaskBinding, select_and_compile_task
from .onboard_history import OnboardInferenceScope
from .planner import normalize_vehicle_class
from .policy_contract import SharedTaskPolicy
from .prompt_resolution import PromptTaskResolution, PromptTaskResolver, runtime_sha256
from .serialization import (
    canonical_sha256,
    optional_real,
    require_int,
    require_real,
    require_sha256,
    require_text,
)
from .task_policy_runtime import TaskPolicyRuntimeBinding
from .task_selection import TaskCandidate


class OnboardEventKind(StrEnum):
    """Bounded events that can change an in-flight high-level decision."""

    TARGET_MOVED = "target_moved"
    TARGET_LOST = "target_lost"
    GPS_DEGRADED = "gps_degraded"
    BATTERY_LOW = "battery_low"
    OBSTACLE_RISK = "obstacle_risk"
    INFERENCE_FAULT = "inference_fault"
    MISSION_COMPLETE = "mission_complete"


class OnboardResponse(StrEnum):
    """High-level proposal consumed by an adapter-owned safety path."""

    EXECUTE = "execute"
    REPLAN = "replan"
    RETURN = "return"
    HOLD = "hold"
    LAND = "land"


class OnboardFlightAction(StrEnum):
    """Model-level navigation intent, before Mission compilation."""

    HOLD_POSITION = "hold_position"
    MOVE_RELATIVE = "move_relative"
    GOTO_RELATIVE = "goto_relative"
    SEARCH = "search"
    FOLLOW_TARGET = "follow_target"
    RETURN_HOME = "return_home"
    LAND = "land"


class OnboardTaskAction(StrEnum):
    """Semantic task step selected by the realtime onboard model."""

    TAKEOFF = "takeoff"
    SEARCH = "search"
    INSPECT_TARGET = "inspect_target"
    MATCH_TARGET = "match_target"
    RECORD_LOCATION = "record_location"
    RETURN_HOME = "return_home"
    REPORT = "report"
    HOLD = "hold"
    LAND = "land"


ONBOARD_MODEL_EVENT_KINDS: tuple[OnboardEventKind, ...] = tuple(OnboardEventKind)
ONBOARD_MODEL_TASK_PHASES: tuple[OnboardTaskAction, ...] = tuple(OnboardTaskAction)
# A search manoeuvre is an information-gathering action: it may proceed with
# a lower model posterior when all hard navigation gates (GPS, obstacle
# distance, geofence and planner validation) are healthy. Target MATCH/RECORD
# and non-search replans retain the stricter confidence gate below. This lets
# the onboard loop continue a left/right sweep instead of deadlocking on the
# first frame with no target, while never lowering the target-confirmation
# threshold used for GPS/RTL.
MIN_SEARCH_REPLAN_CONFIDENCE = 0.25
MIN_REPLAN_CONFIDENCE = 0.55
ONBOARD_MODEL_LEGACY_FEATURE_NAMES: tuple[str, ...] = (
    "battery_remaining_fraction",
    "estimated_flight_time_fraction",
    "ground_speed_fraction",
    "airspeed_fraction",
    "wind_north_fraction",
    "wind_east_fraction",
    "gps_fix_fraction",
    "gps_horizontal_accuracy_fraction",
    "geofence_valid",
    "camera_available",
    "perception_available",
    "map_cache_available",
    "dem_available",
    "event_severity_fraction",
    "target_bearing_fraction",
    "target_distance_fraction",
    "target_confidence",
    "target_match_score",
    "obstacle_bearing_fraction",
    "obstacle_distance_fraction",
    "obstacle_observation_valid",
    "route_progress_fraction",
    *(f"event_{kind.value}" for kind in ONBOARD_MODEL_EVENT_KINDS),
    *(f"phase_{phase.value}" for phase in ONBOARD_MODEL_TASK_PHASES),
)

# r4 extends the existing high-level policy input; it does not create a
# second policy.  The two additional values are structured perception
# attributes supplied by the detector/tracker seam.  ``0.5`` means unknown,
# while 0/1 mean an explicit mismatch/match.  Keeping the old names exported
# lets the adapter read the retained r3 artifact during a staged rollout.
_ONBOARD_MODEL_SCALAR_FEATURE_NAMES: tuple[str, ...] = (
    "battery_remaining_fraction",
    "estimated_flight_time_fraction",
    "ground_speed_fraction",
    "airspeed_fraction",
    "wind_north_fraction",
    "wind_east_fraction",
    "gps_fix_fraction",
    "gps_horizontal_accuracy_fraction",
    "geofence_valid",
    "camera_available",
    "perception_available",
    "map_cache_available",
    "dem_available",
    "event_severity_fraction",
    "target_bearing_fraction",
    "target_distance_fraction",
    "target_confidence",
    "target_match_score",
    "target_class_match",
    "target_color_match",
    "obstacle_bearing_fraction",
    "obstacle_distance_fraction",
    "obstacle_observation_valid",
    "route_progress_fraction",
)
ONBOARD_MODEL_FEATURE_NAMES: tuple[str, ...] = (
    *_ONBOARD_MODEL_SCALAR_FEATURE_NAMES,
    *(f"event_{kind.value}" for kind in ONBOARD_MODEL_EVENT_KINDS),
    *(f"phase_{phase.value}" for phase in ONBOARD_MODEL_TASK_PHASES),
)


@dataclass(frozen=True, slots=True)
class OnboardRuntimeEvent:
    """A source-bound event from perception, navigation or vehicle health."""

    event_id: str
    kind: OnboardEventKind
    observed_at_s: float
    severity: int = 0
    detail: str = ""
    target_bearing_deg: float | None = None
    # ``ned`` is a calibrated world-frame bearing.  ``camera_relative`` is
    # the raw image/camera bearing and must never be silently interpreted as
    # North/East by the model or the GPS recorder.
    target_geometry_frame: str = "ned"
    target_distance_m: float | None = None
    target_north_offset_m: float | None = None
    target_east_offset_m: float | None = None
    target_confidence: float | None = None
    target_match_score: float | None = None
    target_class: str | None = None
    target_color: str | None = None
    target_class_match: bool | None = None
    target_color_match: bool | None = None
    obstacle_bearing_deg: float | None = None
    obstacle_distance_m: float | None = None
    route_progress_fraction: float | None = None
    task_phase: OnboardTaskAction | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", require_text(self.event_id, "event_id"))
        if not isinstance(self.kind, OnboardEventKind):
            raise TypeError("kind must be an OnboardEventKind")
        object.__setattr__(
            self,
            "observed_at_s",
            require_real(self.observed_at_s, "observed_at_s", minimum=0.0),
        )
        object.__setattr__(
            self,
            "severity",
            require_int(self.severity, "severity", minimum=0, maximum=100),
        )
        detail = self.detail.strip() if isinstance(self.detail, str) else self.detail
        object.__setattr__(self, "detail", require_text(detail or self.kind.value, "detail"))
        object.__setattr__(
            self,
            "target_bearing_deg",
            optional_real(
                self.target_bearing_deg, "target_bearing_deg", minimum=-180.0, maximum=180.0
            ),
        )
        if self.target_geometry_frame not in {"ned", "camera_relative"}:
            raise ValueError("target_geometry_frame must be 'ned' or 'camera_relative'")
        if self.target_geometry_frame == "camera_relative" and (
            self.target_north_offset_m is not None or self.target_east_offset_m is not None
        ):
            raise ValueError("camera_relative geometry cannot carry calibrated NED offsets")
        object.__setattr__(
            self,
            "target_distance_m",
            optional_real(
                self.target_distance_m, "target_distance_m", minimum=0.0, maximum=5_000.0
            ),
        )
        for name in ("target_north_offset_m", "target_east_offset_m"):
            object.__setattr__(
                self,
                name,
                optional_real(getattr(self, name), name, minimum=-5_000.0, maximum=5_000.0),
            )
        object.__setattr__(
            self,
            "target_confidence",
            optional_real(self.target_confidence, "target_confidence", minimum=0.0, maximum=1.0),
        )
        object.__setattr__(
            self,
            "target_match_score",
            optional_real(self.target_match_score, "target_match_score", minimum=0.0, maximum=1.0),
        )
        for name in ("target_class", "target_color"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{name} must be a non-empty string when provided")
                object.__setattr__(self, name, value.strip().lower())
        for name in ("target_class_match", "target_color_match"):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise TypeError(f"{name} must be a bool or None")
        object.__setattr__(
            self,
            "obstacle_bearing_deg",
            optional_real(
                self.obstacle_bearing_deg, "obstacle_bearing_deg", minimum=-180.0, maximum=180.0
            ),
        )
        object.__setattr__(
            self,
            "obstacle_distance_m",
            optional_real(
                self.obstacle_distance_m, "obstacle_distance_m", minimum=0.0, maximum=5_000.0
            ),
        )
        object.__setattr__(
            self,
            "route_progress_fraction",
            optional_real(
                self.route_progress_fraction,
                "route_progress_fraction",
                minimum=0.0,
                maximum=1.0,
            ),
        )
        if self.task_phase is not None and not isinstance(self.task_phase, OnboardTaskAction):
            raise TypeError("task_phase must be an OnboardTaskAction or None")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "contract": "multidetect.onboard-runtime-event",
            "event_id": self.event_id,
            "kind": self.kind.value,
            "observed_at_s": self.observed_at_s,
            "severity": self.severity,
            "detail": self.detail,
            "target_bearing_deg": self.target_bearing_deg,
            "target_geometry_frame": self.target_geometry_frame,
            "target_distance_m": self.target_distance_m,
            "target_north_offset_m": self.target_north_offset_m,
            "target_east_offset_m": self.target_east_offset_m,
            "target_confidence": self.target_confidence,
            "target_match_score": self.target_match_score,
            "target_class": self.target_class,
            "target_color": self.target_color,
            "target_class_match": self.target_class_match,
            "target_color_match": self.target_color_match,
            "obstacle_bearing_deg": self.obstacle_bearing_deg,
            "obstacle_distance_m": self.obstacle_distance_m,
            "route_progress_fraction": self.route_progress_fraction,
            "task_phase": None if self.task_phase is None else self.task_phase.value,
            "truth_free": True,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class OnboardFlightPlan:
    """Bounded navigation intent produced by the onboard model.

    Offsets are North/East metres from the current truth-free vehicle position;
    the adapter converts them into a platform-neutral waypoint and then runs
    the existing planner, Mission compiler and safety envelope.  No actuator
    axes or MAVLink fields are part of this object.
    """

    action: OnboardFlightAction
    north_offset_m: float = 0.0
    east_offset_m: float = 0.0
    altitude_m: float | None = None
    speed_mps: float | None = None
    duration_s: float | None = None
    reference_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, OnboardFlightAction):
            raise TypeError("action must be an OnboardFlightAction")
        object.__setattr__(
            self,
            "north_offset_m",
            require_real(self.north_offset_m, "north_offset_m", minimum=-500.0, maximum=500.0),
        )
        object.__setattr__(
            self,
            "east_offset_m",
            require_real(self.east_offset_m, "east_offset_m", minimum=-500.0, maximum=500.0),
        )
        object.__setattr__(
            self,
            "altitude_m",
            optional_real(self.altitude_m, "altitude_m", minimum=0.0, maximum=300.0),
        )
        object.__setattr__(
            self,
            "speed_mps",
            optional_real(self.speed_mps, "speed_mps", minimum=0.0, maximum=50.0),
        )
        object.__setattr__(
            self,
            "duration_s",
            optional_real(self.duration_s, "duration_s", minimum=0.0, maximum=900.0),
        )
        if self.reference_id is not None:
            object.__setattr__(
                self, "reference_id", require_text(self.reference_id, "reference_id")
            )
        if self.action in {
            OnboardFlightAction.HOLD_POSITION,
            OnboardFlightAction.RETURN_HOME,
            OnboardFlightAction.LAND,
        } and (self.north_offset_m != 0.0 or self.east_offset_m != 0.0):
            raise ValueError("safety flight actions cannot carry movement offsets")

    def to_mapping(self) -> dict[str, object]:
        return {
            "action": self.action.value,
            "north_offset_m": self.north_offset_m,
            "east_offset_m": self.east_offset_m,
            "altitude_m": self.altitude_m,
            "speed_mps": self.speed_mps,
            "duration_s": self.duration_s,
            "reference_id": self.reference_id,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> OnboardFlightPlan:
        if not isinstance(value, Mapping):
            raise TypeError("flight_plan must be an object")
        required = {"action", "north_offset_m", "east_offset_m"}
        optional = {"altitude_m", "speed_mps", "duration_s", "reference_id"}
        if set(value) - required - optional or not required <= set(value):
            raise ValueError("flight_plan fields are invalid")
        try:
            action = OnboardFlightAction(require_text(value["action"], "flight_plan.action"))
        except ValueError as exc:
            raise ValueError("flight_plan.action is unsupported") from exc
        return cls(
            action=action,
            north_offset_m=require_real(
                value["north_offset_m"], "flight_plan.north_offset_m", minimum=-500.0, maximum=500.0
            ),
            east_offset_m=require_real(
                value["east_offset_m"], "flight_plan.east_offset_m", minimum=-500.0, maximum=500.0
            ),
            altitude_m=optional_real(
                value.get("altitude_m"), "flight_plan.altitude_m", minimum=0.0, maximum=300.0
            ),
            speed_mps=optional_real(
                value.get("speed_mps"), "flight_plan.speed_mps", minimum=0.0, maximum=50.0
            ),
            duration_s=optional_real(
                value.get("duration_s"), "flight_plan.duration_s", minimum=0.0, maximum=900.0
            ),
            reference_id=(
                None
                if value.get("reference_id") is None
                else require_text(value["reference_id"], "flight_plan.reference_id")
            ),
        )

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class OnboardModelProposal:
    """One high-level response proposed by the Jetson/host realtime model.

    The model may choose a response, but this object never carries actuator
    axes, MAVLink messages, arming authority or a Mission upload command. A
    SIM and a Jetson adapter populate the same identity and latency fields so
    evidence can distinguish a model run from a deterministic fallback.
    """

    response: OnboardResponse
    reason: str
    confidence: float
    model_id: str
    model_version: str
    model_sha256: str
    provider: str
    inference_latency_ms: float
    flight_plan: OnboardFlightPlan | None = None
    task_action: OnboardTaskAction | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.response, OnboardResponse):
            raise TypeError("response must be an OnboardResponse")
        object.__setattr__(self, "reason", require_text(self.reason, "reason"))
        object.__setattr__(
            self,
            "confidence",
            require_real(self.confidence, "confidence", minimum=0.0, maximum=1.0),
        )
        for name in ("model_id", "model_version", "provider"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        object.__setattr__(
            self,
            "model_sha256",
            require_sha256(self.model_sha256, "model_sha256"),
        )
        object.__setattr__(
            self,
            "inference_latency_ms",
            require_real(self.inference_latency_ms, "inference_latency_ms", minimum=0.0),
        )
        if self.flight_plan is not None and not isinstance(self.flight_plan, OnboardFlightPlan):
            raise TypeError("flight_plan must be an OnboardFlightPlan or None")
        if self.task_action is not None and not isinstance(self.task_action, OnboardTaskAction):
            raise TypeError("task_action must be an OnboardTaskAction or None")

    def to_mapping(self) -> dict[str, object]:
        return {
            "response": self.response.value,
            "reason": self.reason,
            "confidence": self.confidence,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "model_sha256": self.model_sha256,
            "provider": self.provider,
            "inference_latency_ms": self.inference_latency_ms,
            "flight_plan": None if self.flight_plan is None else self.flight_plan.to_mapping(),
            "task_action": None if self.task_action is None else self.task_action.value,
            "truth_free": True,
            "policy_applied_to_actuator": False,
        }


@dataclass(frozen=True, slots=True)
class OnboardModelInput:
    """Truth-free structured input shared by SIM and Jetson model runners."""

    context: ContextSnapshot
    event: OnboardRuntimeEvent

    def __post_init__(self) -> None:
        if not isinstance(self.context, ContextSnapshot):
            raise TypeError("context must be a ContextSnapshot")
        if self.context.schema_version != 1:
            raise ValueError(
                "retained 38/40-feature models require v1 context semantics; "
                "v2 unknown/source-aware inputs need an explicitly versioned model adapter"
            )
        if not isinstance(self.event, OnboardRuntimeEvent):
            raise TypeError("event must be an OnboardRuntimeEvent")

    def to_mapping(self) -> dict[str, object]:
        features = self.feature_vector()
        return {
            "schema_version": 1,
            "contract": "multidetect.onboard-model-input",
            "context": self.context.to_mapping(),
            "event": self.event.to_mapping(),
            "feature_names": list(ONBOARD_MODEL_FEATURE_NAMES),
            "features": list(features),
            "truth_free": True,
        }

    def feature_vector(self) -> tuple[float, ...]:
        """Return the stable normalized vector consumed by ONNX/TensorRT runners."""

        context = self.context
        event = self.event
        scalar = (
            context.battery_remaining_pct / 100.0,
            context.estimated_flight_time_s / 3_600.0,
            context.ground_speed_mps / 50.0,
            context.airspeed_mps / 50.0,
            context.wind_north_mps / 20.0,
            context.wind_east_mps / 20.0,
            context.gps_fix_type / 6.0,
            context.gps_horizontal_accuracy_m / 100.0,
            float(context.geofence_valid),
            float(context.camera_available),
            float(context.perception_available),
            float(context.map_cache_available),
            float(context.dem_available),
            event.severity / 100.0,
            (
                (event.target_bearing_deg or 0.0) / 180.0
                if event.target_geometry_frame == "ned"
                else 0.0
            ),
            (event.target_distance_m or 0.0) / 5_000.0,
            event.target_confidence or 0.0,
            event.target_match_score or 0.0,
            0.5 if event.target_class_match is None else float(event.target_class_match),
            0.5 if event.target_color_match is None else float(event.target_color_match),
            (event.obstacle_bearing_deg or 0.0) / 180.0,
            (5_000.0 if event.obstacle_distance_m is None else event.obstacle_distance_m) / 5_000.0,
            float(event.obstacle_distance_m is not None),
            event.route_progress_fraction or 0.0,
        )
        event_one_hot = tuple(float(event.kind is kind) for kind in ONBOARD_MODEL_EVENT_KINDS)
        phase_one_hot = tuple(
            float(event.task_phase is phase) for phase in ONBOARD_MODEL_TASK_PHASES
        )
        return scalar + event_one_hot + phase_one_hot

    def legacy_feature_vector(self) -> tuple[float, ...]:
        """Return the retained r3 vector for a staged old-model rollout."""

        vector = self.feature_vector()
        # The two r4 attributes sit after target_match_score in the scalar
        # prefix.  Remove them while retaining the exact historical order.
        scalar_width = len(_ONBOARD_MODEL_SCALAR_FEATURE_NAMES)
        scalar = vector[:scalar_width]
        tail = vector[scalar_width:]
        return scalar[:18] + scalar[20:] + tail

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class OnboardModelOutput:
    """Typed output expected from a realtime high-level model backend."""

    response: OnboardResponse
    reason: str
    confidence: float
    flight_plan: OnboardFlightPlan | None = None
    task_action: OnboardTaskAction | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.response, OnboardResponse):
            raise TypeError("response must be an OnboardResponse")
        object.__setattr__(self, "reason", require_text(self.reason, "reason"))
        object.__setattr__(
            self,
            "confidence",
            require_real(self.confidence, "confidence", minimum=0.0, maximum=1.0),
        )
        if self.flight_plan is not None and not isinstance(self.flight_plan, OnboardFlightPlan):
            raise TypeError("flight_plan must be an OnboardFlightPlan or None")
        if self.task_action is not None and not isinstance(self.task_action, OnboardTaskAction):
            raise TypeError("task_action must be an OnboardTaskAction or None")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> OnboardModelOutput:
        if not isinstance(value, Mapping):
            raise TypeError("model output must be a mapping or OnboardModelOutput")
        required = {"response", "reason", "confidence"}
        optional = {"flight_plan", "task_action"}
        if not required <= set(value) or set(value) - required - optional:
            raise ValueError("model output fields are invalid")
        raw_response = value["response"]
        try:
            response = (
                raw_response
                if isinstance(raw_response, OnboardResponse)
                else OnboardResponse(require_text(raw_response, "response"))
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("model output response is unsupported") from exc
        raw_task_action = value.get("task_action")
        if raw_task_action is None:
            task_action = None
        else:
            try:
                task_action = (
                    raw_task_action
                    if isinstance(raw_task_action, OnboardTaskAction)
                    else OnboardTaskAction(require_text(raw_task_action, "task_action"))
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("model output task_action is unsupported") from exc
        return cls(
            response=response,
            reason=require_text(value["reason"], "reason"),
            confidence=require_real(value["confidence"], "confidence", minimum=0.0, maximum=1.0),
            flight_plan=(
                None
                if value.get("flight_plan") is None
                else OnboardFlightPlan.from_mapping(value["flight_plan"])
            ),
            task_action=task_action,
        )


@runtime_checkable
class OnboardModelRunner(Protocol):
    """Backend-neutral inference contract for ONNX, TensorRT or a test runner."""

    def infer(self, model_input: OnboardModelInput) -> OnboardModelOutput | Mapping[str, object]:
        """Run one inference without transport or actuator access."""


class ModelBackedDecisionProvider:
    """Adapt one realtime model runner into :class:`OnboardDecisionProvider`.

    The runner is the only replaceable implementation between a Gazebo/4060
    process and a Jetson process.  It may use ONNX Runtime, TensorRT or a
    deterministic test backend, but it must return the same three-field output
    contract.  Model identity is supplied by the deployment manifest and is
    copied into every proposal; it is never inferred from an unverified path.
    """

    __slots__ = (
        "runner",
        "model_id",
        "model_version",
        "model_sha256",
        "provider",
        "_clock",
    )

    def __init__(
        self,
        runner: OnboardModelRunner,
        *,
        model_id: str,
        model_version: str,
        model_sha256: str,
        provider: str,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        if not callable(getattr(runner, "infer", None)):
            raise TypeError("runner must expose a callable infer method")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.runner = runner
        self.model_id = require_text(model_id, "model_id")
        self.model_version = require_text(model_version, "model_version")
        self.model_sha256 = require_sha256(model_sha256, "model_sha256")
        self.provider = require_text(provider, "provider")
        self._clock = clock

    def propose(
        self,
        context: ContextSnapshot,
        event: OnboardRuntimeEvent,
    ) -> OnboardModelProposal:
        return self._propose(context, event, self.runner.infer)

    @property
    def requires_fresh_phase_observation(self) -> bool:
        """Scoped temporal runners cannot count a phase update as a sensor tick."""

        return callable(getattr(self.runner, "infer_scoped", None))

    @property
    def maximum_phase_observation_gap_s(self) -> float | None:
        if not self.requires_fresh_phase_observation:
            return None
        history = getattr(self.runner, "history", None)
        gap = require_real(getattr(history, "max_gap_s", None), "history max_gap_s", minimum=0)
        if gap == 0:
            raise ValueError("history max_gap_s must be positive")
        return gap

    def reset_for_session(self, scope: OnboardInferenceScope, *, reason: str) -> bool:
        """Explicitly rebind history only after the adapter commits a task switch."""

        reset = getattr(self.runner, "reset_history", None)
        if not callable(reset):
            if self.requires_fresh_phase_observation:
                raise RuntimeError("scoped runner requires an explicit history reset method")
            return False
        if not isinstance(scope, OnboardInferenceScope):
            raise TypeError("scope must be an OnboardInferenceScope")
        reset(scope=scope, reason=require_text(reason, "history reset reason"))
        return True

    def propose_for_session(
        self,
        context: ContextSnapshot,
        event: OnboardRuntimeEvent,
        scope: OnboardInferenceScope,
    ) -> OnboardModelProposal:
        """Use scoped inference only when the runner explicitly supports it."""

        scoped = getattr(self.runner, "infer_scoped", None)
        if not callable(scoped):
            return self.propose(context, event)
        if not isinstance(scope, OnboardInferenceScope):
            raise TypeError("scope must be an OnboardInferenceScope")
        return self._propose(context, event, lambda model_input: scoped(model_input, scope))

    def observe_for_session(
        self,
        context: ContextSnapshot,
        event: OnboardRuntimeEvent,
        scope: OnboardInferenceScope,
    ) -> bool:
        """Collect history only; legacy single-frame runners remain untouched."""

        observer = getattr(self.runner, "observe_scoped", None)
        if not callable(observer):
            return False
        if not isinstance(scope, OnboardInferenceScope):
            raise TypeError("scope must be an OnboardInferenceScope")
        changed = observer(OnboardModelInput(context, event), scope)
        if type(changed) is not bool:
            raise TypeError("scoped observation must return bool")
        return changed

    def _propose(
        self,
        context: ContextSnapshot,
        event: OnboardRuntimeEvent,
        infer: Callable[[OnboardModelInput], OnboardModelOutput | Mapping[str, object]],
    ) -> OnboardModelProposal:
        model_input = OnboardModelInput(context, event)
        started = require_real(self._clock(), "model inference clock")
        raw_output = infer(model_input)
        elapsed_ms = require_real(
            (require_real(self._clock(), "model inference clock") - started) * 1000.0,
            "model inference latency_ms",
            minimum=0,
        )
        output = (
            raw_output
            if isinstance(raw_output, OnboardModelOutput)
            else OnboardModelOutput.from_mapping(raw_output)
        )
        return OnboardModelProposal(
            response=output.response,
            reason=output.reason,
            confidence=output.confidence,
            model_id=self.model_id,
            model_version=self.model_version,
            model_sha256=self.model_sha256,
            provider=self.provider,
            inference_latency_ms=elapsed_ms,
            flight_plan=output.flight_plan,
            task_action=output.task_action,
        )


@runtime_checkable
class OnboardDecisionProvider(Protocol):
    """Realtime high-level decision seam implemented by Jetson or SIM."""

    def propose(
        self,
        context: ContextSnapshot,
        event: OnboardRuntimeEvent,
    ) -> OnboardModelProposal:
        """Return one high-level proposal for the current observation."""


@dataclass(frozen=True, slots=True)
class OnboardDecision:
    """One hash-bound OnboardAI proposal; it has no transport authority."""

    prompt_sha256: str
    context_sha256: str
    event_sha256: str | None
    response: OnboardResponse
    reason: str
    binding: TaskPolicyRuntimeBinding
    resolver_id: str | None = None
    resolution_sha256: str | None = None
    model_proposal: OnboardModelProposal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "prompt_sha256", require_sha256(self.prompt_sha256, "prompt_sha256")
        )
        object.__setattr__(
            self, "context_sha256", require_sha256(self.context_sha256, "context_sha256")
        )
        if self.event_sha256 is not None:
            object.__setattr__(
                self, "event_sha256", require_sha256(self.event_sha256, "event_sha256")
            )
        if not isinstance(self.response, OnboardResponse):
            raise TypeError("response must be an OnboardResponse")
        object.__setattr__(self, "reason", require_text(self.reason, "reason"))
        if self.resolver_id is not None:
            object.__setattr__(self, "resolver_id", require_text(self.resolver_id, "resolver_id"))
        if self.resolution_sha256 is not None:
            object.__setattr__(
                self,
                "resolution_sha256",
                require_sha256(self.resolution_sha256, "resolution_sha256"),
            )
        if (self.resolver_id is None) != (self.resolution_sha256 is None):
            raise ValueError("resolver_id and resolution_sha256 must be provided together")
        if not isinstance(self.binding, TaskPolicyRuntimeBinding):
            raise TypeError("binding must be a TaskPolicyRuntimeBinding")
        if self.model_proposal is not None and not isinstance(
            self.model_proposal, OnboardModelProposal
        ):
            raise TypeError("model_proposal must be an OnboardModelProposal or None")
        if self.response in {OnboardResponse.EXECUTE, OnboardResponse.REPLAN}:
            if not self.binding.mission_bound or not self.binding.planner_revalidated:
                raise ValueError("execute/replan requires a revalidated bound mission")
        elif self.binding.mission_bound:
            raise ValueError("safety response cannot bind a mission")

    @property
    def selected_task_id(self) -> str | None:
        return self.binding.selected_task_id

    @property
    def planner_revalidated(self) -> bool:
        return self.binding.planner_revalidated

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "contract": "multidetect.onboard-decision",
            "prompt_sha256": self.prompt_sha256,
            "context_sha256": self.context_sha256,
            "event_sha256": self.event_sha256,
            "response": self.response.value,
            "reason": self.reason,
            "resolver_id": self.resolver_id,
            "resolution_sha256": self.resolution_sha256,
            "model_proposal": (
                None if self.model_proposal is None else self.model_proposal.to_mapping()
            ),
            "decision_source": ("model" if self.model_proposal is not None else "safety_baseline"),
            "selected_task_id": self.selected_task_id,
            "planner_revalidated": self.planner_revalidated,
            "mission_bound": self.binding.mission_bound,
            "mission_started": False,
            "policy_applied_to_actuator": False,
            "truth_free": True,
            "policy_runtime_binding": self.binding.to_mapping(),
        }


_EVENT_RESPONSE: dict[OnboardEventKind, OnboardResponse] = {
    OnboardEventKind.GPS_DEGRADED: OnboardResponse.RETURN,
    OnboardEventKind.BATTERY_LOW: OnboardResponse.RETURN,
    OnboardEventKind.OBSTACLE_RISK: OnboardResponse.HOLD,
    OnboardEventKind.INFERENCE_FAULT: OnboardResponse.RETURN,
}
_REPLAN_EVENTS = frozenset(
    {
        OnboardEventKind.TARGET_MOVED,
        OnboardEventKind.TARGET_LOST,
        OnboardEventKind.MISSION_COMPLETE,
    }
)


class OnboardAIExecutor:
    """Run the same high-level task policy in SIM, HIL and REAL adapters.

    This class deliberately accepts already-resolved ``TaskCandidate`` values
    instead of parsing arbitrary language in the flight loop.  A prompt
    resolver upstream may use the same object in Gazebo and Jetson; once
    resolved, the hash-bound planner/policy/mission path is identical.
    """

    __slots__ = (
        "prompt",
        "shared_policy",
        "decision_provider",
        "allow_deterministic_baseline",
        "vehicle_class",
        "airframe_profile_hash",
        "_last_context",
        "_last_event_at_s",
        "_active_task_id",
        "_active_binding",
        "_prompt_resolution",
        "_session_id",
        "_clock_epoch",
    )

    def __init__(
        self,
        prompt: PromptEnvelope,
        shared_policy: SharedTaskPolicy,
        vehicle_class: str,
        airframe_profile_hash: str,
        decision_provider: OnboardDecisionProvider | None = None,
        *,
        allow_deterministic_baseline: bool = False,
        session_id: str | None = None,
        clock_epoch: str | None = None,
    ) -> None:
        if not isinstance(prompt, PromptEnvelope):
            raise TypeError("prompt must be a PromptEnvelope")
        if not isinstance(shared_policy, SharedTaskPolicy):
            raise TypeError("shared_policy must be a SharedTaskPolicy")
        self.prompt = prompt
        self.shared_policy = shared_policy
        self.vehicle_class = normalize_vehicle_class(vehicle_class)
        self.airframe_profile_hash = require_sha256(airframe_profile_hash, "airframe_profile_hash")
        if shared_policy.artifact.airframe_profile_hash != self.airframe_profile_hash:
            raise ValueError("shared policy airframe profile mismatch")
        if decision_provider is not None and not callable(
            getattr(decision_provider, "propose", None)
        ):
            raise TypeError("decision_provider must expose a callable propose method")
        if not isinstance(allow_deterministic_baseline, bool):
            raise TypeError("allow_deterministic_baseline must be bool")
        self._last_context: ContextSnapshot | None = None
        self.decision_provider = decision_provider
        self.allow_deterministic_baseline = allow_deterministic_baseline
        self._last_event_at_s: float | None = None
        self._active_task_id: str | None = None
        # Keep the last successfully selected task binding so a realtime
        # model movement can replan the active task locally.  Re-running the
        # complete prompt route here is incorrect: a long task may become
        # infeasible after battery is spent while a short, bounded search
        # step is still safe and compilable.  The local step is independently
        # revalidated by OnboardMissionAdapter/compile_model_flight_plan.
        self._active_binding: TaskPolicyRuntimeBinding | None = None
        self._prompt_resolution: PromptTaskResolution | None = None
        self._session_id = (
            str(uuid4()) if session_id is None else require_text(session_id, "session_id")
        )
        if str(UUID(self._session_id)) != self._session_id:
            raise ValueError("session_id must be a canonical UUID")
        # This default is a process/executor epoch, not a sensor-clock claim.
        self._clock_epoch = (
            self._session_id if clock_epoch is None else require_text(clock_epoch, "clock_epoch")
        )

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def active_task_id(self) -> str | None:
        return self._active_task_id

    def reset_model_history_after_task_switch(
        self, context: ContextSnapshot, previous_task_id: str | None
    ) -> bool:
        """Called only after the Mission adapter acknowledges its full transaction.

        A proposal or standalone executor.step is not a transaction commit.
        Same-task navigation revisions keep the existing observation history.
        """

        if (
            previous_task_id is None
            or self._active_task_id is None
            or self._active_task_id == previous_task_id
        ):
            return False
        reset = getattr(self.decision_provider, "reset_for_session", None)
        if not callable(reset):
            if getattr(self.decision_provider, "requires_fresh_phase_observation", False):
                raise RuntimeError("temporal provider requires an explicit history reset seam")
            return False
        changed = reset(self.model_session_scope(context), reason="committed_task_transition")
        if type(changed) is not bool:
            raise TypeError("scoped history reset must return bool")
        if not changed and getattr(
            self.decision_provider, "requires_fresh_phase_observation", False
        ):
            raise RuntimeError("temporal provider did not acknowledge its history reset")
        return changed

    def model_session_scope(self, context: ContextSnapshot) -> OnboardInferenceScope:
        """Bind the immutable prompt/vehicle/task and explicit clock epoch."""

        if self._active_task_id is None:
            raise ValueError("an active task is required for scoped inference")
        return OnboardInferenceScope(
            session_id=self._session_id,
            prompt_sha256=self.prompt.canonical_hash,
            aircraft_id=self.prompt.aircraft_id,
            selected_task_id=self._active_task_id,
            airframe_profile_id=context.airframe_profile_id,
            airframe_profile_sha256=self.airframe_profile_hash,
            vehicle_class=self.vehicle_class,
            home_sha256=context.home_position.canonical_hash,
            clock_epoch=self._clock_epoch,
        )

    def observe_model_input(self, context: ContextSnapshot, event: OnboardRuntimeEvent) -> bool:
        """Sample suppressed observations without proposing or replacing a Mission.

        Call through OnboardMissionEventLoop so this shares the step lock.
        Input/scope/time failures propagate; no fallback policy is selected.
        """

        observer = getattr(self.decision_provider, "observe_for_session", None)
        if not callable(observer):
            return False
        self._validate_model_input(context, event)
        changed = observer(context, event, self.model_session_scope(context))
        if type(changed) is not bool:
            raise TypeError("scoped observation must return bool")
        if changed:
            self._last_context = context
            self._last_event_at_s = event.observed_at_s
        return changed

    def _validate_model_input(self, context: ContextSnapshot, event: OnboardRuntimeEvent) -> None:
        if not isinstance(event, OnboardRuntimeEvent):
            raise TypeError("event must be an OnboardRuntimeEvent")
        self._validate_context(context)
        if self._last_context is None:
            raise ValueError("prepare must be called before step")
        if event.observed_at_s < self._last_context.captured_at_s:
            raise ValueError("event predates the current context")
        if event.observed_at_s > context.captured_at_s:
            raise ValueError("event is newer than the current context")
        if self._last_event_at_s is not None and event.observed_at_s < self._last_event_at_s:
            raise ValueError("event timestamp regressed")

    def _validate_context(self, context: ContextSnapshot) -> None:
        if not isinstance(context, ContextSnapshot):
            raise TypeError("context must be a ContextSnapshot")
        if context.airframe_profile_hash != self.airframe_profile_hash:
            raise ValueError("context airframe profile mismatch")
        if self._last_context is not None:
            if context.captured_at_s < self._last_context.captured_at_s:
                raise ValueError("context timestamp regressed")
            if context.home_position != self._last_context.home_position:
                raise ValueError("home position changed during onboard session")

    @staticmethod
    def _validate_inputs(
        candidates: Sequence[TaskCandidate], runtimes: Mapping[str, object]
    ) -> tuple[tuple[TaskCandidate, ...], Mapping[str, object]]:
        if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes, bytearray)):
            raise TypeError("candidates must be a sequence")
        entries = tuple(candidates)
        if not entries or any(not isinstance(item, TaskCandidate) for item in entries):
            raise ValueError("candidates must be a non-empty TaskCandidate sequence")
        ids = [entry.task.task_id for entry in entries]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate task IDs must be unique")
        if not isinstance(runtimes, Mapping):
            raise TypeError("runtimes must be a mapping")
        missing = [task_id for task_id in ids if task_id not in runtimes]
        if missing:
            raise ValueError(f"missing compile runtime for {missing[0]}")
        return entries, dict(runtimes)

    def _safety_decision(
        self,
        context: ContextSnapshot,
        event: OnboardRuntimeEvent,
        response: OnboardResponse,
        reason: str,
        *,
        model_proposal: OnboardModelProposal | None = None,
    ) -> OnboardDecision:
        binding = TaskPolicyRuntimeBinding.from_decision(
            context=context,
            airframe_profile_hash=self.airframe_profile_hash,
            vehicle_class=self.vehicle_class,
            decision=None,
            selected=None,
            policy_artifact_sha256=self.shared_policy.artifact_sha256,
            ranker_sha256=self.shared_policy.ranker.canonical_hash,
            reason=reason,
        )
        return OnboardDecision(
            self.prompt.canonical_hash,
            context.canonical_hash,
            event.canonical_hash,
            response,
            reason,
            binding,
            model_proposal=model_proposal,
        )

    def _select(
        self,
        context: ContextSnapshot,
        candidates: Sequence[TaskCandidate],
        runtimes: Mapping[str, object],
        *,
        event: OnboardRuntimeEvent | None,
        response: OnboardResponse,
        mission_id: str | None = None,
        model_proposal: OnboardModelProposal | None = None,
    ) -> OnboardDecision:
        entries, runtime_map = self._validate_inputs(candidates, runtimes)
        binding: CampaignTaskBinding = select_and_compile_task(
            entries,
            context,
            self.vehicle_class,
            runtimes=runtime_map,
            airframe_profile_hash=self.airframe_profile_hash,
            shared_policy=self.shared_policy,
            mission_id=mission_id,
        )
        if binding.selected is None or binding.compilation is None:
            # Once a task is active, a model-proposed movement is a local
            # replan of that task, not a new selection of the entire prompt
            # route.  This matters late in a sortie: the original route may
            # no longer fit the remaining energy budget even though the
            # bounded search step does.  The adapter still runs the same
            # canonical model-flight-plan compiler and PX4 transaction, so
            # this branch does not grant transport or actuator authority.
            proposal_plan = model_proposal.flight_plan if model_proposal is not None else None
            active = self._active_binding
            active_task_id = self._active_task_id
            active_in_scope = (
                active is not None
                and active_task_id is not None
                and active.selected_task_id == active_task_id
                and any(item.task.task_id == active_task_id for item in entries)
                and active.policy_artifact_sha256 == self.shared_policy.artifact_sha256
            )
            if (
                binding.decision is not None
                and binding.decision.reason == "no_feasible_task"
                and active_in_scope
                and proposal_plan is not None
                and proposal_plan.action
                in {
                    OnboardFlightAction.MOVE_RELATIVE,
                    OnboardFlightAction.GOTO_RELATIVE,
                    OnboardFlightAction.SEARCH,
                    OnboardFlightAction.FOLLOW_TARGET,
                }
            ):
                runtime_binding = replace(
                    active,
                    context_sha256=context.canonical_hash,
                    reason="active_task_local_replan_after_route_budget",
                    produced_at_s=context.captured_at_s,
                    planner_revalidated=True,
                    mission_bound=True,
                )
                decision = OnboardDecision(
                    self.prompt.canonical_hash,
                    context.canonical_hash,
                    event.canonical_hash if event is not None else None,
                    response,
                    "active_task_local_replan_after_route_budget",
                    runtime_binding,
                    model_proposal=model_proposal,
                )
                self._active_task_id = active_task_id
                return decision
            reason = event.detail if event is not None and event.detail else "policy_abstained"
            safety_binding = binding.to_runtime_binding(
                airframe_profile_hash=self.airframe_profile_hash,
                ranker_sha256=self.shared_policy.ranker.canonical_hash,
                reason=reason,
            )
            return OnboardDecision(
                self.prompt.canonical_hash,
                context.canonical_hash,
                event.canonical_hash if event is not None else None,
                OnboardResponse.HOLD,
                reason,
                safety_binding,
                model_proposal=model_proposal,
            )
        reason = (
            event.detail
            if event is not None and event.detail
            else (
                "mission_selected" if response is OnboardResponse.EXECUTE else "mission_replanned"
            )
        )
        runtime_binding = binding.to_runtime_binding(
            airframe_profile_hash=self.airframe_profile_hash,
            ranker_sha256=self.shared_policy.ranker.canonical_hash,
            reason=reason,
        )
        self._active_binding = runtime_binding
        decision = OnboardDecision(
            self.prompt.canonical_hash,
            context.canonical_hash,
            event.canonical_hash if event is not None else None,
            response,
            reason,
            runtime_binding,
            model_proposal=model_proposal,
        )
        self._active_task_id = decision.selected_task_id
        return decision

    def _annotate_resolution(self, decision: OnboardDecision) -> OnboardDecision:
        resolution = self._prompt_resolution
        if resolution is None:
            return decision
        return replace(
            decision,
            resolver_id=resolution.resolver_id,
            resolution_sha256=resolution.canonical_hash,
        )

    @staticmethod
    def _return_envelope_valid(context: ContextSnapshot) -> bool:
        """Keep the non-learned return envelope deliberately small and strict."""

        return (
            context.geofence_valid
            and context.gps_fix_type >= MAVLINK_QUALIFIED_FIX_TYPE_MIN
            and context.gps_horizontal_accuracy_m <= GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M
        )

    def _propose_event(
        self,
        context: ContextSnapshot,
        event: OnboardRuntimeEvent,
        candidates: Sequence[TaskCandidate] | None,
        runtimes: Mapping[str, object] | None,
        *,
        mission_id: str | None,
    ) -> OnboardDecision:
        """Apply a model proposal, then pass it through the fixed safety envelope."""

        provider = self.decision_provider
        if provider is None:  # pragma: no cover - caller selects baseline path
            raise RuntimeError("model decision provider is not configured")
        try:
            scoped = getattr(provider, "propose_for_session", None)
            proposal = (
                scoped(context, event, self.model_session_scope(context))
                if callable(scoped)
                else provider.propose(context, event)
            )
        except Exception:
            # A model timeout/exception is a safety event, not permission to
            # silently run the old fixed RETURN table.
            return self._safety_decision(
                context,
                event,
                OnboardResponse.HOLD,
                "model_provider_fault",
            )
        if not isinstance(proposal, OnboardModelProposal):
            return self._safety_decision(
                context,
                event,
                OnboardResponse.HOLD,
                "model_provider_invalid_proposal",
            )
        if (
            proposal.flight_plan is not None
            and proposal.flight_plan.action
            in {
                OnboardFlightAction.MOVE_RELATIVE,
                OnboardFlightAction.GOTO_RELATIVE,
                OnboardFlightAction.SEARCH,
                OnboardFlightAction.FOLLOW_TARGET,
            }
            and event.obstacle_distance_m is None
        ):
            return self._safety_decision(
                context,
                event,
                OnboardResponse.HOLD,
                "model_navigation_vetoed_unknown_obstacle",
                model_proposal=proposal,
            )
        if (
            proposal.flight_plan is not None
            and proposal.flight_plan.action
            in {
                OnboardFlightAction.MOVE_RELATIVE,
                OnboardFlightAction.GOTO_RELATIVE,
                OnboardFlightAction.SEARCH,
                OnboardFlightAction.FOLLOW_TARGET,
            }
            and event.target_distance_m is not None
            and event.target_geometry_frame != "ned"
        ):
            return self._safety_decision(
                context,
                event,
                OnboardResponse.HOLD,
                "model_navigation_vetoed_uncalibrated_geometry",
                model_proposal=proposal,
            )
        replan_confidence_gate = MIN_REPLAN_CONFIDENCE
        if (
            proposal.task_action in {OnboardTaskAction.SEARCH, OnboardTaskAction.INSPECT_TARGET}
            and proposal.flight_plan is not None
            and proposal.flight_plan.action
            in {
                OnboardFlightAction.MOVE_RELATIVE,
                OnboardFlightAction.GOTO_RELATIVE,
                OnboardFlightAction.SEARCH,
            }
        ):
            replan_confidence_gate = MIN_SEARCH_REPLAN_CONFIDENCE
        if (
            proposal.response is OnboardResponse.REPLAN
            and proposal.confidence < replan_confidence_gate
        ):
            return self._safety_decision(
                context,
                event,
                OnboardResponse.HOLD,
                "model_replan_below_confidence_gate",
                model_proposal=proposal,
            )
        if proposal.response is OnboardResponse.REPLAN:
            if candidates is None and runtimes is None and self._prompt_resolution is not None:
                candidates = self._prompt_resolution.candidates
                runtimes = self._prompt_resolution.runtimes
            elif (candidates is None) != (runtimes is None):
                raise ValueError("replanning candidates and runtimes must be provided together")
            if candidates is None or runtimes is None:
                return self._safety_decision(
                    context,
                    event,
                    OnboardResponse.HOLD,
                    "model_replan_inputs_unavailable",
                    model_proposal=proposal,
                )
            self._validate_prompt_scope(candidates, runtimes)
            if self._prompt_resolution is not None:
                candidates = self._prompt_resolution.candidates
                runtimes = self._prompt_resolution.runtimes
            return self._select(
                context,
                candidates,
                runtimes,
                event=event,
                response=OnboardResponse.REPLAN,
                mission_id=mission_id,
                model_proposal=proposal,
            )
        if proposal.response is OnboardResponse.RETURN and not self._return_envelope_valid(context):
            return self._safety_decision(
                context,
                event,
                OnboardResponse.HOLD,
                "model_return_vetoed_by_safety_envelope",
                model_proposal=proposal,
            )
        if proposal.response in {
            OnboardResponse.RETURN,
            OnboardResponse.HOLD,
            OnboardResponse.LAND,
        }:
            return self._safety_decision(
                context,
                event,
                proposal.response,
                proposal.reason,
                model_proposal=proposal,
            )
        return self._safety_decision(
            context,
            event,
            OnboardResponse.HOLD,
            "model_response_not_allowed_for_event",
            model_proposal=proposal,
        )

    def _validate_prompt_scope(
        self,
        candidates: Sequence[TaskCandidate],
        runtimes: Mapping[str, object],
    ) -> None:
        resolution = self._prompt_resolution
        if resolution is None:
            return
        entries, runtime_map = self._validate_inputs(candidates, runtimes)
        expected_candidates = tuple(
            (item.task.task_id, item.task.canonical_hash, item.priority)
            for item in resolution.candidates
        )
        actual_candidates = tuple(
            (item.task.task_id, item.task.canonical_hash, item.priority) for item in entries
        )
        if actual_candidates != expected_candidates:
            raise ValueError("step candidates are outside the resolved prompt catalog")
        for task_id, expected_hash in resolution.runtime_hashes.items():
            runtime = runtime_map.get(task_id)
            if runtime is None or runtime_sha256(runtime) != expected_hash:
                raise ValueError("step runtime is outside the resolved prompt catalog")

    def prepare(
        self,
        context: ContextSnapshot,
        candidates: Sequence[TaskCandidate],
        runtimes: Mapping[str, object],
        *,
        mission_id: str | None = None,
    ) -> OnboardDecision:
        """Resolve the initial PromptEnvelope into one compiled Mission proposal."""

        if self._last_context is not None:
            raise ValueError("onboard session is already prepared")
        self._validate_context(context)
        decision = self._select(
            context,
            candidates,
            runtimes,
            event=None,
            response=OnboardResponse.EXECUTE,
            mission_id=mission_id,
        )
        self._last_context = context
        return decision

    def prepare_from_prompt(
        self,
        context: ContextSnapshot,
        resolver: PromptTaskResolver,
        *,
        mission_id: str | None = None,
    ) -> OnboardDecision:
        """Resolve the immutable prompt through an approved catalog once.

        The resolver runs outside the realtime event loop.  After resolution,
        this method enters the exact same ``prepare`` path used by Gazebo and
        Jetson adapters; an unknown prompt or aircraft binding fails closed.
        """

        if not isinstance(resolver, PromptTaskResolver):
            raise TypeError("resolver must be a PromptTaskResolver")
        resolution = resolver.resolve(self.prompt)
        decision = self.prepare(
            context, resolution.candidates, resolution.runtimes, mission_id=mission_id
        )
        self._prompt_resolution = resolution
        return self._annotate_resolution(decision)

    def step(
        self,
        context: ContextSnapshot,
        event: OnboardRuntimeEvent,
        candidates: Sequence[TaskCandidate] | None = None,
        runtimes: Mapping[str, object] | None = None,
        *,
        mission_id: str | None = None,
    ) -> OnboardDecision:
        """Consume one fresh runtime event and propose continue/replan/safety response.

        ``mission_id`` is supplied by the adapter that owns Mission
        replacement.  It makes every replan a distinct hash-bound PX4 Mission
        revision instead of silently reusing the initial task-derived ID.
        """

        self._validate_model_input(context, event)
        if self.decision_provider is not None:
            decision = self._propose_event(
                context,
                event,
                candidates,
                runtimes,
                mission_id=mission_id,
            )
        elif self.allow_deterministic_baseline and event.kind in _EVENT_RESPONSE:
            decision = self._safety_decision(
                context,
                event,
                _EVENT_RESPONSE[event.kind],
                event.detail or event.kind.value,
            )
        elif self.allow_deterministic_baseline and event.kind in _REPLAN_EVENTS:
            if candidates is None and runtimes is None and self._prompt_resolution is not None:
                candidates = self._prompt_resolution.candidates
                runtimes = self._prompt_resolution.runtimes
            elif (candidates is None) != (runtimes is None):
                raise ValueError("replanning candidates and runtimes must be provided together")
            if candidates is None or runtimes is None:
                raise ValueError("replanning requires candidates and runtimes or a resolved prompt")
            self._validate_prompt_scope(candidates, runtimes)
            if self._prompt_resolution is not None:
                # Consume the immutable snapshot after checking compatibility
                # inputs; callers cannot mutate the checked object during compilation.
                candidates = self._prompt_resolution.candidates
                runtimes = self._prompt_resolution.runtimes
            decision = self._select(
                context,
                candidates,
                runtimes,
                event=event,
                response=OnboardResponse.REPLAN,
                mission_id=mission_id,
            )
        else:
            # A missing model is not permission to resurrect the historical
            # event-to-response table.  Production callers must inject the
            # same reviewed model runner used in SIM and Jetson; the explicit
            # baseline flag exists only for retained compatibility evidence.
            decision = self._safety_decision(
                context,
                event,
                OnboardResponse.HOLD,
                "model_provider_unconfigured",
            )
        self._last_context = context
        self._last_event_at_s = event.observed_at_s
        return self._annotate_resolution(decision)


__all__ = [
    "OnboardAIExecutor",
    "OnboardDecision",
    "OnboardDecisionProvider",
    "OnboardEventKind",
    "OnboardFlightAction",
    "OnboardFlightPlan",
    "OnboardTaskAction",
    "OnboardModelInput",
    "ONBOARD_MODEL_LEGACY_FEATURE_NAMES",
    "ONBOARD_MODEL_EVENT_KINDS",
    "ONBOARD_MODEL_FEATURE_NAMES",
    "ONBOARD_MODEL_TASK_PHASES",
    "OnboardModelOutput",
    "OnboardModelProposal",
    "OnboardModelRunner",
    "ModelBackedDecisionProvider",
    "OnboardResponse",
    "OnboardRuntimeEvent",
]
