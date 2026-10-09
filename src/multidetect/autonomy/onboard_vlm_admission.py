"""Development-only admission of source-relative VLM intent at a newer state.

The model's original request and answer remain source-bound. Only a host-owned
geodetic transform changes the effective offsets. This is not sensor coverage,
transport permission, a default policy, or evidence of physical flight.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field, replace

from ..geolocation_accuracy import wgs84_distance_m
from ..mavlink_gps import GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M, MAVLINK_QUALIFIED_FIX_TYPE_MIN
from ..mission_planning import PlannedMissionCompilation, parse_mission_compile_runtime
from ..target_geolocation import WGS84_FLATTENING, wgs84_forward_from_ned
from .contracts import ContextSnapshot, GeoPoint, ResolvedTaskSpec
from .onboard_agent import (
    MIN_SEARCH_REPLAN_CONFIDENCE,
    OnboardEventKind,
    OnboardFlightAction,
    OnboardFlightPlan,
    OnboardModelProposal,
    OnboardResponse,
    OnboardRuntimeEvent,
    OnboardTaskAction,
)
from .onboard_history import OnboardInferenceScope
from .onboard_multimodal import OnboardMultimodalScope, Scope
from .onboard_navigation import compile_model_flight_plan
from .onboard_vlm_runtime_contract import (
    OnboardVlmRuntimeRequest,
    parse_onboard_vlm_runtime_response,
)
from .onboard_vlm_worker import OnboardVlmWorkerResult
from .serialization import (
    canonical_sha256,
    require_int,
    require_real,
    require_sha256,
    require_text,
    strict_json_loads,
)


class OnboardVlmAdmissionError(ValueError):
    """A source intent cannot be proved admissible at the execution snapshot."""


def _scope(value: Scope) -> OnboardMultimodalScope:
    if isinstance(value, OnboardInferenceScope):
        return OnboardMultimodalScope.from_inference_scope(value)
    if isinstance(value, OnboardMultimodalScope):
        return value
    raise TypeError("scope must be a typed onboard scope")


def _distance(a: GeoPoint, b: GeoPoint) -> float:
    return wgs84_distance_m(
        latitude1_deg=a.latitude_deg,
        longitude1_deg=a.longitude_deg,
        latitude2_deg=b.latitude_deg,
        longitude2_deg=b.longitude_deg,
    )


def _wire_target(target: GeoPoint) -> tuple[int, int, float | None]:
    return round(target.latitude_deg * 1e7), round(target.longitude_deg * 1e7), target.altitude_m


def wgs84_inverse_offsets(origin: GeoPoint, target: GeoPoint) -> tuple[float, float]:
    """Bounded Vincenty azimuth plus the canonical ellipsoid distance, no fallback."""
    if not isinstance(origin, GeoPoint) or not isinstance(target, GeoPoint):
        raise TypeError("inverse inputs must be GeoPoint")
    distance = _distance(origin, target)
    if distance > 1_000.0 or not (
        -90 < origin.latitude_deg < 90 and -90 < target.latitude_deg < 90
    ):
        raise OnboardVlmAdmissionError("inverse is outside the local supported domain")
    if distance == 0:
        return 0.0, 0.0
    u1 = math.atan((1 - WGS84_FLATTENING) * math.tan(math.radians(origin.latitude_deg)))
    u2 = math.atan((1 - WGS84_FLATTENING) * math.tan(math.radians(target.latitude_deg)))
    s1, c1, s2, c2 = math.sin(u1), math.cos(u1), math.sin(u2), math.cos(u2)
    delta = math.radians((target.longitude_deg - origin.longitude_deg + 180) % 360 - 180)
    lam = delta
    for _ in range(100):
        sl, cl = math.sin(lam), math.cos(lam)
        sine = math.hypot(c2 * sl, c1 * s2 - s1 * c2 * cl)
        if sine == 0:
            raise OnboardVlmAdmissionError("inverse has undefined bearing")
        cosine = s1 * s2 + c1 * c2 * cl
        sigma = math.atan2(sine, cosine)
        alpha_sine = c1 * c2 * sl / sine
        alpha_cos2 = max(0.0, 1 - alpha_sine**2)
        sigma2_cos = 0.0 if alpha_cos2 <= 1e-15 else cosine - 2 * s1 * s2 / alpha_cos2
        coefficient = (
            WGS84_FLATTENING / 16 * alpha_cos2 * (4 + WGS84_FLATTENING * (4 - 3 * alpha_cos2))
        )
        next_lam = delta + (1 - coefficient) * WGS84_FLATTENING * alpha_sine * (
            sigma
            + coefficient * sine * (sigma2_cos + coefficient * cosine * (-1 + 2 * sigma2_cos**2))
        )
        if abs(next_lam - lam) <= 1e-12:
            lam = next_lam
            break
        lam = next_lam
    else:
        raise OnboardVlmAdmissionError("inverse azimuth did not converge")
    azimuth = math.atan2(c2 * math.sin(lam), c1 * s2 - s1 * c2 * math.cos(lam))
    north, east = distance * math.cos(azimuth), distance * math.sin(azimuth)
    projected = wgs84_forward_from_ned(
        latitude_deg=origin.latitude_deg,
        longitude_deg=origin.longitude_deg,
        north_offset_m=north,
        east_offset_m=east,
    )
    # Reprojection must preserve the actual integer wire target, not merely be close.
    if (round(projected[0] * 1e7), round(projected[1] * 1e7)) != _wire_target(target)[:2]:
        raise OnboardVlmAdmissionError("inverse reprojection changes the wire target")
    return north, east


@dataclass(frozen=True, slots=True)
class RouteClearanceEvidence:
    """Host-reviewed segment coverage, never produced by the VLM.

    The source JSON and its hash prove byte identity, not source authentication
    or sensor truth. A caller must explicitly trust the actual sensor receiver.
    No current receiver in this development path produces qualified coverage.
    """

    context_sha256: str
    event_sha256: str
    target: GeoPoint
    observed_at_s: float
    verified_path_length_m: float
    minimum_clearance_m: float
    source_id: str
    source_receipt_bytes: bytes
    source_receipt_sha256: str
    wire_hash: str

    def claims_mapping(self) -> dict[str, object]:
        return {
            "contract": "multidetect.host-route-clearance-development-v1",
            "context_sha256": self.context_sha256,
            "event_sha256": self.event_sha256,
            "target": self.target.to_mapping(),
            "observed_at_s": self.observed_at_s,
            "verified_path_length_m": self.verified_path_length_m,
            "minimum_clearance_m": self.minimum_clearance_m,
            "source_id": self.source_id,
            "coverage": "entire_compiled_mission",
            "wire_hash": self.wire_hash,
            "truth_free": True,
        }

    def __post_init__(self) -> None:
        require_sha256(self.context_sha256, "clearance context")
        require_sha256(self.event_sha256, "clearance event")
        require_sha256(self.source_receipt_sha256, "clearance original source")
        require_sha256(self.wire_hash, "clearance Mission wire hash")
        require_text(self.source_id, "clearance source_id")
        if (
            not isinstance(self.target, GeoPoint)
            or self.target.altitude_reference != "relative_home"
        ):
            raise OnboardVlmAdmissionError("clearance target requires relative-home GeoPoint")
        for name in ("observed_at_s", "verified_path_length_m", "minimum_clearance_m"):
            require_real(getattr(self, name), name, minimum=0)
        if not isinstance(self.source_receipt_bytes, bytes) or not (
            0 < len(self.source_receipt_bytes) <= 16 * 1024
        ):
            raise OnboardVlmAdmissionError("clearance requires bounded original JSON bytes")
        if hashlib.sha256(self.source_receipt_bytes).hexdigest() != self.source_receipt_sha256:
            raise OnboardVlmAdmissionError("clearance original bytes hash mismatch")
        if (
            strict_json_loads(self.source_receipt_bytes, name="clearance source")
            != self.claims_mapping()
        ):
            raise OnboardVlmAdmissionError("clearance source does not prove exact bound claims")

    def to_mapping(self) -> dict[str, object]:
        return {**self.claims_mapping(), "source_receipt_sha256": self.source_receipt_sha256}


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    context: ContextSnapshot
    event: OnboardRuntimeEvent
    scope: Scope
    mission_sha256: str
    mission_revision: int
    receipt_revision: int
    clearance: RouteClearanceEvidence | None = None
    policy_artifact_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.context, ContextSnapshot) or not isinstance(
            self.event, OnboardRuntimeEvent
        ):
            raise TypeError("snapshot requires typed context and event")
        scope = _scope(self.scope)
        scope.validate_context(self.context)
        if scope.vehicle_class != "multirotor" or scope.selected_task_id is None:
            raise OnboardVlmAdmissionError("snapshot requires known multirotor task scope")
        if self.context.captured_at_s != self.event.observed_at_s:
            raise OnboardVlmAdmissionError("snapshot must carry contemporaneous real event/context")
        require_sha256(self.mission_sha256, "mission_sha256")
        require_int(self.mission_revision, "mission_revision", minimum=0)
        require_int(self.receipt_revision, "receipt_revision", minimum=0)
        require_sha256(self.policy_artifact_sha256, "shared policy artifact identity")
        if self.clearance is not None and not isinstance(self.clearance, RouteClearanceEvidence):
            raise TypeError("clearance must be typed or unknown")

    def to_mapping(self) -> dict[str, object]:
        return {
            "context_sha256": self.context.canonical_hash,
            "event_sha256": self.event.canonical_hash,
            "scope_sha256": _scope(self.scope).canonical_hash,
            "captured_at_s": self.context.captured_at_s,
            "mission_sha256": self.mission_sha256,
            "mission_revision": self.mission_revision,
            "receipt_revision": self.receipt_revision,
            "policy_artifact_sha256": self.policy_artifact_sha256,
            "clearance": None if self.clearance is None else self.clearance.to_mapping(),
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class SourceAnchoredIntent:
    request: OnboardVlmRuntimeRequest
    result: OnboardVlmWorkerResult
    source: ExecutionSnapshot
    task: ResolvedTaskSpec
    runtime_json: str
    model_id: str
    model_version: str
    model_sha256: str
    expected_generation: int
    target: GeoPoint = field(init=False)

    @property
    def runtime(self) -> dict[str, object]:
        value = strict_json_loads(self.runtime_json, name="anchored runtime")
        if not isinstance(value, dict):
            raise OnboardVlmAdmissionError("runtime must be original JSON object")
        return value

    def __post_init__(self) -> None:
        if not isinstance(self.request, OnboardVlmRuntimeRequest) or not isinstance(
            self.result, OnboardVlmWorkerResult
        ):
            raise TypeError("anchor requires exact typed request and worker result")
        if not isinstance(self.source, ExecutionSnapshot) or not isinstance(
            self.task, ResolvedTaskSpec
        ):
            raise TypeError("anchor requires typed source and task")
        self.request.validate_binding()
        for name in ("model_id", "model_version"):
            require_text(getattr(self, name), name)
        require_sha256(self.model_sha256, "model_sha256")
        require_int(self.expected_generation, "expected_generation", minimum=1)
        if (
            type(self.result.generation) is not int
            or self.result.generation != self.expected_generation
            or self.result.request_sha256 != self.request.canonical_hash
        ):
            raise OnboardVlmAdmissionError(
                "worker generation/request does not match submitted source"
            )
        frame = self.request.model_input.frames[-1]
        if (
            frame.context is None
            or frame.context.canonical_hash != self.source.context.canonical_hash
            or _scope(self.request.model_input.scope) != _scope(self.source.scope)
            or self.request.source_event is None
            or self.request.source_event.canonical_hash != self.source.event.canonical_hash
            or _scope(self.source.scope).selected_task_id != self.task.task_id
        ):
            raise OnboardVlmAdmissionError(
                "anchor source context/event/scope/task differs from request"
            )
        source_context = self.source.context
        if (
            source_context.gps_fix_type < MAVLINK_QUALIFIED_FIX_TYPE_MIN
            or source_context.gps_horizontal_accuracy_m > GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M
            or source_context.aircraft_position.altitude_reference != "relative_home"
        ):
            raise OnboardVlmAdmissionError("source navigation cannot anchor a metric intent")
        parsed = parse_onboard_vlm_runtime_response(
            self.result.raw_output,
            self.request,
            current_scope=self.request.model_input.scope,
            completed_at_s=self.result.completed_at_s,
            inference_latency_ms=self.result.inference_latency_ms,
        )
        if parsed != self.result.response:
            raise OnboardVlmAdmissionError("typed result differs from original raw response")
        output, plan = parsed.output, parsed.output.flight_plan
        if (
            output.response is not OnboardResponse.REPLAN
            or output.task_action is not OnboardTaskAction.SEARCH
            or plan is None
            or plan.action not in {OnboardFlightAction.SEARCH, OnboardFlightAction.MOVE_RELATIVE}
        ):
            raise OnboardVlmAdmissionError("this version only admits fixed SEARCH/MOVE intent")
        if output.confidence < MIN_SEARCH_REPLAN_CONFIDENCE:
            raise OnboardVlmAdmissionError("source movement is below existing confidence gate")
        if not isinstance(self.runtime_json, str) or len(self.runtime_json.encode()) > 64 * 1024:
            raise OnboardVlmAdmissionError("runtime JSON is unbounded")
        geometry, safety = parse_mission_compile_runtime(self.runtime, "multirotor")
        if geometry.home.canonical_hash != self.source.context.home_position.canonical_hash:
            raise OnboardVlmAdmissionError("runtime HOME differs from source")
        latitude, longitude = wgs84_forward_from_ned(
            latitude_deg=self.source.context.aircraft_position.latitude_deg,
            longitude_deg=self.source.context.aircraft_position.longitude_deg,
            north_offset_m=plan.north_offset_m,
            east_offset_m=plan.east_offset_m,
        )
        requested_altitude = (
            geometry.takeoff_altitude_m if plan.altitude_m is None else plan.altitude_m
        )
        altitude = min(
            safety.maximum_relative_altitude_m,
            max(safety.minimum_relative_altitude_m, requested_altitude),
        )
        object.__setattr__(self, "target", GeoPoint(latitude, longitude, altitude, "relative_home"))

    def to_mapping(self) -> dict[str, object]:
        return {
            "contract": "multidetect.onboard-vlm-source-anchor-development-v1",
            "request_sha256": self.request.canonical_hash,
            "raw_response_sha256": self.result.response.raw_response_sha256,
            "source": self.source.to_mapping(),
            "original_output": self.result.response.to_mapping()["output"],
            "completed_at_s": self.result.completed_at_s,
            "inference_latency_ms": self.result.inference_latency_ms,
            "generation": self.expected_generation,
            "task_sha256": self.task.canonical_hash,
            "runtime_sha256": canonical_sha256(self.runtime),
            "model_id": self.model_id,
            "model_version": self.model_version,
            "model_sha256": self.model_sha256,
            "anchored_target": self.target.to_mapping(),
            "source_timestamps_rewritten": False,
            "execution_authority": False,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class ExecutionAdmissionPolicy:
    max_source_age_s: float = 15.0
    max_current_age_s: float = 1.0
    max_origin_displacement_m: float = 100.0
    minimum_clearance_m: float = 5.0
    trusted_clearance_source_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, ceiling in (
            ("max_source_age_s", 30.0),
            ("max_current_age_s", 2.0),
            ("max_origin_displacement_m", 100.0),
            ("minimum_clearance_m", 100.0),
        ):
            require_real(getattr(self, name), name, minimum=0.001, maximum=ceiling)
        if self.minimum_clearance_m < 5:
            raise OnboardVlmAdmissionError("clearance margin cannot weaken the development minimum")
        if not isinstance(self.trusted_clearance_source_ids, tuple) or len(
            set(self.trusted_clearance_source_ids)
        ) != len(self.trusted_clearance_source_ids):
            raise OnboardVlmAdmissionError(
                "trusted clearance IDs must be an immutable unique tuple"
            )
        for value in self.trusted_clearance_source_ids:
            require_text(value, "trusted source")

    def to_mapping(self) -> dict[str, object]:
        return {
            "max_source_age_s": self.max_source_age_s,
            "max_current_age_s": self.max_current_age_s,
            "max_origin_displacement_m": self.max_origin_displacement_m,
            "minimum_clearance_m": self.minimum_clearance_m,
            "trusted_clearance_source_ids": list(self.trusted_clearance_source_ids),
        }


@dataclass(frozen=True, slots=True)
class ExecutionAdmission:
    anchor: SourceAnchoredIntent
    current: ExecutionSnapshot
    effective_plan: OnboardFlightPlan
    compilation: PlannedMissionCompilation
    admitted_at_s: float
    policy: ExecutionAdmissionPolicy

    def validate_binding(self, *, now_s: float) -> None:
        reopened = admit_anchored_intent(
            self.anchor,
            self.current,
            now_s=now_s,
            mission_id=self.compilation.compiled_mission.executable_mission.mission_id,
            policy=self.policy,
        )
        # Revalidation time is new evidence, not a mutation of original admission.
        if (
            replace(reopened, admitted_at_s=self.admitted_at_s).canonical_hash
            != self.canonical_hash
        ):
            raise OnboardVlmAdmissionError(
                "execution admission differs from canonical revalidation"
            )

    def proposal(self) -> OnboardModelProposal:
        output = self.anchor.result.response.output
        return OnboardModelProposal(
            output.response,
            output.reason,
            output.confidence,
            self.anchor.model_id,
            self.anchor.model_version,
            self.anchor.model_sha256,
            "development-vlm-source-to-execution",
            self.anchor.result.inference_latency_ms,
            self.effective_plan,
            output.task_action,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "contract": "multidetect.onboard-vlm-execution-admission-development-v1",
            "source_anchor_sha256": self.anchor.canonical_hash,
            "source": self.anchor.to_mapping(),
            "execution_snapshot": self.current.to_mapping(),
            "effective_plan": self.effective_plan.to_mapping(),
            "effective_plan_origin": "host_geodetic_reprojection",
            "admitted_at_s": self.admitted_at_s,
            "admission_policy": self.policy.to_mapping(),
            "plan_sha256": self.compilation.plan.canonical_hash,
            "mission_sha256": self.compilation.executable_mission_hash,
            "wire_hash": self.compilation.wire_hash,
            "planner_revalidated": True,
            "model_saw_execution_context": False,
            "execution_authority": False,
            "autonomous_flight_verified": False,
            "candidate_promoted": False,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


_DEFAULT_ADMISSION_POLICY = ExecutionAdmissionPolicy()


def admit_anchored_intent(
    anchor: SourceAnchoredIntent,
    current: ExecutionSnapshot,
    *,
    now_s: float,
    mission_id: str,
    policy: ExecutionAdmissionPolicy = _DEFAULT_ADMISSION_POLICY,
) -> ExecutionAdmission:
    """Validate both snapshots, preserve the goal, and recompile at current state."""
    if not isinstance(anchor, SourceAnchoredIntent) or not isinstance(current, ExecutionSnapshot):
        raise TypeError("admission requires typed anchor and execution snapshot")
    if not isinstance(policy, ExecutionAdmissionPolicy):
        raise TypeError("policy must be ExecutionAdmissionPolicy")
    require_real(now_s, "now_s", minimum=0)
    # Re-open all original bindings; frozen types alone are not a provenance check.
    reopened = replace(anchor)
    if reopened.canonical_hash != anchor.canonical_hash:
        raise OnboardVlmAdmissionError("source anchor mutated")
    anchor.request.model_input.validate_freshness(now_s)
    source = anchor.source
    if _scope(source.scope) != _scope(current.scope) or (
        source.mission_sha256 != current.mission_sha256
        or source.mission_revision != current.mission_revision
        or source.receipt_revision != current.receipt_revision
        or source.policy_artifact_sha256 != current.policy_artifact_sha256
    ):
        raise OnboardVlmAdmissionError("scope/Mission/receipt changed since source inference")
    if not (
        source.context.captured_at_s <= current.context.captured_at_s <= now_s
        and anchor.result.completed_at_s <= now_s
        and now_s - source.context.captured_at_s <= policy.max_source_age_s
        and now_s - current.context.captured_at_s <= policy.max_current_age_s
    ):
        raise OnboardVlmAdmissionError("source/current/host time is stale or regressed")
    context, event = current.context, current.event
    _, runtime_policy = parse_mission_compile_runtime(anchor.runtime, "multirotor")
    if (
        context.aircraft_position.altitude_reference != "relative_home"
        or context.aircraft_position.altitude_m is None
        or not runtime_policy.minimum_relative_altitude_m
        <= context.aircraft_position.altitude_m
        <= runtime_policy.maximum_relative_altitude_m
    ):
        raise OnboardVlmAdmissionError("current height is unknown or outside the flight envelope")
    if (
        _distance(source.context.aircraft_position, context.aircraft_position)
        > policy.max_origin_displacement_m
    ):
        raise OnboardVlmAdmissionError("aircraft moved beyond source admission displacement")
    if not (
        context.geofence_valid
        and context.gps_fix_type >= MAVLINK_QUALIFIED_FIX_TYPE_MIN
        and context.gps_horizontal_accuracy_m <= GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M
        and context.camera_available
        and context.perception_available
        and context.map_cache_available
        and context.dem_available
    ):
        raise OnboardVlmAdmissionError("current navigation/resources are unqualified")
    if (
        event.kind
        in {
            OnboardEventKind.GPS_DEGRADED,
            OnboardEventKind.BATTERY_LOW,
            OnboardEventKind.OBSTACLE_RISK,
            OnboardEventKind.INFERENCE_FAULT,
            OnboardEventKind.MISSION_COMPLETE,
        }
        or event.obstacle_distance_m is None
        or event.obstacle_distance_m == 5000
        or event.obstacle_distance_m < policy.minimum_clearance_m
        or (event.target_distance_m is not None and event.target_geometry_frame != "ned")
    ):
        raise OnboardVlmAdmissionError("current event cannot admit old movement intent")
    clearance = current.clearance
    if clearance is None or clearance.source_id not in policy.trusted_clearance_source_ids:
        raise OnboardVlmAdmissionError("current host-reviewed route clearance is unknown")
    effective, compilation = compile_anchored_intent_at_current(
        anchor, current, mission_id=mission_id
    )
    replace(clearance)  # recheck exact original source bytes, not a cached accepted flag
    if (
        clearance.context_sha256 != context.canonical_hash
        or clearance.event_sha256 != event.canonical_hash
        or _wire_target(clearance.target) != _wire_target(anchor.target)
        or not 0 <= now_s - clearance.observed_at_s <= policy.max_current_age_s
        or clearance.observed_at_s > context.captured_at_s
        or clearance.minimum_clearance_m < policy.minimum_clearance_m
        or clearance.wire_hash != compilation.wire_hash
        or clearance.verified_path_length_m
        < minimum_compiled_path_length_m(compilation, context.aircraft_position)
        + 2 * context.gps_horizontal_accuracy_m
        + policy.minimum_clearance_m
    ):
        raise OnboardVlmAdmissionError("route clearance does not cover current anchored segment")
    return ExecutionAdmission(anchor, current, effective, compilation, now_s, policy)


def minimum_compiled_path_length_m(
    compilation: PlannedMissionCompilation, origin: GeoPoint
) -> float:
    """A 3-D geometric lower bound, not a prediction of actual flight dynamics.

    Coverage must independently include every wire command and its manoeuvre
    envelope. This length alone cannot prove obstacle clearance. The retained
    full-lifecycle compiler includes HOME takeoff, loiter and landing too.
    """
    if origin.altitude_reference != "relative_home" or origin.altitude_m is None:
        raise OnboardVlmAdmissionError("route origin requires known relative-home height")
    previous, minimum_length = origin, 0.0
    for item in compilation.compiled_mission.wire_items:
        if item.frame != 6 or item.command not in {16, 19, 21, 22}:
            raise OnboardVlmAdmissionError("unsupported clearance Mission geometry")
        point = GeoPoint(item.x / 1e7, item.y / 1e7, item.z, "relative_home")
        minimum_length += math.hypot(
            _distance(previous, point), point.altitude_m - previous.altitude_m
        )
        if item.command == 19:
            minimum_length += 2 * abs(item.param3)
        previous = point
    return minimum_length


def compile_anchored_intent_at_current(
    anchor: SourceAnchoredIntent, current: ExecutionSnapshot, *, mission_id: str
) -> tuple[OnboardFlightPlan, PlannedMissionCompilation]:
    """Prepare geometry for sensor review; compilation alone grants no admission."""
    context = current.context
    north, east = wgs84_inverse_offsets(context.aircraft_position, anchor.target)
    original = anchor.result.response.output.flight_plan
    effective = replace(
        original, north_offset_m=north, east_offset_m=east, altitude_m=anchor.target.altitude_m
    )
    compilation = compile_model_flight_plan(
        anchor.task,
        context,
        "multirotor",
        runtime=anchor.runtime,
        flight_plan=effective,
        airframe_profile_hash=context.airframe_profile_hash,
        mission_id=require_text(mission_id, "mission_id"),
    )
    waypoints = tuple(
        item for item in compilation.compiled_mission.wire_items if item.command == 16
    )
    if len(waypoints) != 1 or (waypoints[0].x, waypoints[0].y, waypoints[0].z) != _wire_target(
        anchor.target
    ):
        raise OnboardVlmAdmissionError("compiled Mission does not preserve the anchored target")
    if compilation.plan.context_snapshot_hash != context.canonical_hash:
        raise OnboardVlmAdmissionError("canonical plan does not bind current execution context")
    return effective, compilation
