"""Pure, source-target-preserving multirotor continuation authoring.

This is not the full-sortie ``PlannedMissionCompilation`` contract. It emits
current-position WAYPOINT(0), fixed-target WAYPOINT(1), finite LOITER_TIME(2).
The parent object is only typed provenance: independently verified airborne,
readback, cursor, clearance and permission evidence is still required to fly.
The assumed speeds/wait/return altitude are conservative development budget
policy, not calibrated aircraft performance or an implemented watchdog.
"""

from __future__ import annotations

import math
import struct
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any

from ..geolocation_accuracy import wgs84_distance_m
from ..mission_planning import parse_mission_compile_runtime
from ..px4_mission import (
    CompiledPx4Mission,
    MultirotorMissionGeometry,
    Px4MultirotorSafetyPolicy,
    _validate_plan_and_geometry,
    compile_px4_mission_items,
)
from .contracts import (
    ContextSnapshot,
    ExecutableMission,
    GeoPoint,
    MissionCommand,
    MissionFrame,
    MissionItem,
    PlanAction,
    PlanStep,
    PlatformNeutralPlan,
    ResolvedTaskSpec,
)
from .planner import (
    _DETECTION_OVERHEAD_S,
    PlannerPolicy,
    PlanningEstimate,
    _directional_travel_duration,
    _initial_bearing_deg,
    estimate_task,
    normalize_vehicle_class,
    plan_task,
)
from .serialization import (
    canonical_json,
    canonical_sha256,
    require_int,
    require_real,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)


class ContinuationCompileError(ValueError):
    """The supplied facts cannot produce a bounded continuation segment."""


CONTINUATION_ADAPTER_ID = "px4-multirotor-continuation-v1"
CONTINUATION_ADAPTER_HASH = canonical_sha256(
    {
        "adapter_id": CONTINUATION_ADAPTER_ID,
        "frame": "MAV_FRAME_GLOBAL_RELATIVE_ALT_INT",
        "commands": [16, 16, 19],
        "resume_sequence": 1,
        "seq0": "latest_actual_position_not_home_or_takeoff",
        "seq1": "source_fixed_wgs84_target",
        "terminal": "finite_loiter_then_px4_end_of_mission_hold",
        "authority": "pure_compilation_no_execution_grant",
    }
)


class _Canonical:
    __slots__ = ()

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @property
    def canonical_json(self) -> str:
        return canonical_json(self.to_mapping())

    def to_mapping(self) -> dict[str, Any]:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class ContinuationParentBinding(_Canonical):
    """Identity facts only; constructing this is not proof of being in air."""

    parent_mission_id: str
    parent_mission_sha256: str
    parent_wire_sha256: str
    parent_journal_sha256: str
    airframe_profile_sha256: str
    home_position: GeoPoint
    observed_current_sequence: int
    observed_at_s: float
    source_system: int = 1
    source_component: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "parent_mission_id", require_text(self.parent_mission_id, "parent_mission_id")
        )
        for name in (
            "parent_mission_sha256",
            "parent_wire_sha256",
            "parent_journal_sha256",
            "airframe_profile_sha256",
        ):
            object.__setattr__(self, name, require_sha256(getattr(self, name), name))
        if not isinstance(self.home_position, GeoPoint):
            raise ContinuationCompileError("parent home_position must be GeoPoint")
        object.__setattr__(
            self,
            "observed_current_sequence",
            require_int(
                self.observed_current_sequence, "observed_current_sequence", minimum=0, maximum=63
            ),
        )
        object.__setattr__(
            self, "observed_at_s", require_real(self.observed_at_s, "observed_at_s", minimum=0.0)
        )
        for name in ("source_system", "source_component"):
            object.__setattr__(
                self, name, require_int(getattr(self, name), name, minimum=1, maximum=1)
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            name: (value.to_mapping() if isinstance(value, GeoPoint) else value)
            for name, value in ((item.name, getattr(self, item.name)) for item in fields(self))
        }

    @classmethod
    def from_mapping(cls, raw: object) -> ContinuationParentBinding:
        value = strict_object(
            raw,
            name="ContinuationParentBinding",
            required=frozenset(item.name for item in fields(cls)),
        )
        return cls(**{**value, "home_position": GeoPoint.from_mapping(value["home_position"])})


@dataclass(frozen=True, slots=True)
class ContinuationCompilePolicy(_Canonical):
    planner: PlannerPolicy = field(default_factory=PlannerPolicy)
    maximum_context_age_s: float = 1.0
    maximum_parent_age_s: float = 1.0
    assumed_climb_rate_mps: float = 2.0
    assumed_descent_rate_mps: float = 1.0
    assumed_return_relative_altitude_m: float = 60.0
    post_segment_wait_budget_s: float = 30.0

    def __post_init__(self) -> None:
        if not isinstance(self.planner, PlannerPolicy):
            raise ContinuationCompileError("planner must be PlannerPolicy")
        for item in fields(self):
            if item.name != "planner":
                value = require_real(getattr(self, item.name), item.name, minimum=0.001)
                object.__setattr__(self, item.name, value)
        ceilings = {
            "maximum_context_age_s": 1.0,
            "maximum_parent_age_s": 1.0,
            "assumed_climb_rate_mps": 2.0,
            "assumed_descent_rate_mps": 1.0,
        }
        floors = {
            "assumed_return_relative_altitude_m": 60.0,
            "post_segment_wait_budget_s": 30.0,
        }
        if any(getattr(self, name) > limit for name, limit in ceilings.items()) or any(
            getattr(self, name) < limit for name, limit in floors.items()
        ):
            raise ContinuationCompileError(
                "development continuation policy may only tighten defaults"
            )
        default = PlannerPolicy()
        planner_floors = (
            "minimum_gps_fix_type",
            "minimum_geometry_confidence",
            "minimum_battery_margin_pct",
            "minimum_speed_mps",
            "takeoff_duration_s",
            "landing_duration_s",
            "vtol_transition_duration_s",
            "duration_safety_factor",
        )
        planner_ceilings = (
            "maximum_gps_horizontal_accuracy_m",
            "maximum_planning_speed_mps",
            "multirotor_cruise_speed_mps",
            "vtol_cruise_speed_mps",
            "fixed_wing_cruise_speed_mps",
        )
        if any(
            getattr(self.planner, name) < getattr(default, name) for name in planner_floors
        ) or any(getattr(self.planner, name) > getattr(default, name) for name in planner_ceilings):
            raise ContinuationCompileError("development planner policy may only tighten defaults")
        if self.planner.multirotor_cruise_speed_mps > self.planner.maximum_planning_speed_mps:
            raise ContinuationCompileError("assumed cruise speed exceeds planner policy")
        if self.planner.landing_duration_s <= 0:
            raise ContinuationCompileError("return landing duration must be positive")

    def to_mapping(self) -> dict[str, Any]:
        return {
            item.name: asdict(self.planner) if item.name == "planner" else getattr(self, item.name)
            for item in fields(self)
        }

    @classmethod
    def from_mapping(cls, raw: object) -> ContinuationCompilePolicy:
        value = strict_object(
            raw,
            name="ContinuationCompilePolicy",
            required=frozenset(item.name for item in fields(cls)),
        )
        planner = strict_object(
            value["planner"],
            name="PlannerPolicy",
            required=frozenset(item.name for item in fields(PlannerPolicy)),
        )
        return cls(**{**value, "planner": PlannerPolicy(**planner)})


@dataclass(frozen=True, slots=True)
class ContinuationBudget(_Canonical):
    outbound_distance_m: float
    return_distance_m: float
    gps_and_envelope_distance_buffer_m: float
    outbound_travel_duration_s: float
    return_travel_duration_s: float
    buffer_travel_duration_s: float
    segment_climb_m: float
    segment_descent_m: float
    return_climb_m: float
    return_descent_m: float
    vertical_duration_s: float
    loiter_duration_s: float
    post_segment_wait_budget_s: float
    landing_duration_s: float
    detection_overhead_duration_s: float
    assumed_return_relative_altitude_m: float
    duration_safety_factor: float
    estimated_duration_s: float
    available_duration_s: float
    return_reserve_pct: float
    battery_margin_pct: float

    def __post_init__(self) -> None:
        for item in fields(self):
            object.__setattr__(
                self, item.name, require_real(getattr(self, item.name), item.name, minimum=0.0)
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "physical_energy_calibration_verified": False,
            "watchdog_implemented": False,
            "return_altitude_is_verified_px4_parameter": False,
        }


def _distance(left: GeoPoint, right: GeoPoint) -> float:
    return wgs84_distance_m(
        latitude1_deg=left.latitude_deg,
        longitude1_deg=left.longitude_deg,
        latitude2_deg=right.latitude_deg,
        longitude2_deg=right.longitude_deg,
    )


def _fresh(stamp: float, now: float, maximum: float, name: str) -> None:
    if not 0.0 <= now - stamp <= maximum:
        raise ContinuationCompileError(f"{name} is future or expired")


def _travel(
    left: GeoPoint, right: GeoPoint, context: ContextSnapshot, policy: PlannerPolicy
) -> float:
    distance = _distance(left, right)
    if distance <= 1.0e-9:
        return 0.0
    return _directional_travel_duration(
        ((distance, _initial_bearing_deg(left, right)),),
        airspeed_mps=policy.multirotor_cruise_speed_mps,
        wind_north_mps=context.wind_north_mps,
        wind_east_mps=context.wind_east_mps,
        minimum_progress_mps=policy.minimum_speed_mps,
    )


@dataclass(frozen=True, slots=True)
class ContinuationCompilation(_Canonical):
    """Inputs plus recomputed artifacts, never an execution authorization."""

    task: ResolvedTaskSpec
    current_context: ContextSnapshot
    target: GeoPoint
    runtime_json: str
    mission_id: str
    parent: ContinuationParentBinding
    source_intent_sha256: str
    compiled_at_s: float
    policy: ContinuationCompilePolicy = field(default_factory=ContinuationCompilePolicy)
    vehicle_class: str = "multirotor"
    plan: PlatformNeutralPlan = field(init=False)
    budget: ContinuationBudget = field(init=False)
    original_task_estimate: PlanningEstimate = field(init=False)
    compiled_mission: CompiledPx4Mission = field(init=False)

    def __post_init__(self) -> None:
        if normalize_vehicle_class(self.vehicle_class) != "multirotor":
            raise ContinuationCompileError("continuation supports only multirotor")
        object.__setattr__(self, "vehicle_class", "multirotor")
        if not isinstance(self.task, ResolvedTaskSpec) or not isinstance(
            self.current_context, ContextSnapshot
        ):
            raise ContinuationCompileError("typed task and current ContextSnapshot required")
        if self.current_context.schema_version != 1:
            raise ContinuationCompileError(
                "retained continuation contract requires v1 context; "
                "v2 capability/source semantics require explicit continuation migration"
            )
        if not isinstance(self.target, GeoPoint) or not isinstance(
            self.parent, ContinuationParentBinding
        ):
            raise ContinuationCompileError("typed target and parent binding required")
        if not isinstance(self.policy, ContinuationCompilePolicy):
            raise ContinuationCompileError("typed continuation policy required")
        object.__setattr__(self, "mission_id", require_text(self.mission_id, "mission_id"))
        object.__setattr__(
            self,
            "source_intent_sha256",
            require_sha256(self.source_intent_sha256, "source_intent_sha256"),
        )
        object.__setattr__(
            self, "compiled_at_s", require_real(self.compiled_at_s, "compiled_at_s", minimum=0.0)
        )
        if self.mission_id == self.parent.parent_mission_id:
            raise ContinuationCompileError("child Mission ID cannot reuse parent Mission ID")
        runtime = strict_json_loads(self.runtime_json, name="continuation runtime")
        geometry, safety = parse_mission_compile_runtime(runtime, "multirotor")
        if not isinstance(geometry, MultirotorMissionGeometry) or not isinstance(
            safety, Px4MultirotorSafetyPolicy
        ):
            raise ContinuationCompileError("multirotor runtime required")
        object.__setattr__(self, "runtime_json", canonical_json(runtime))
        context, policy, parent = self.current_context, self.policy, self.parent
        _fresh(
            context.captured_at_s,
            self.compiled_at_s,
            policy.maximum_context_age_s,
            "current context",
        )
        _fresh(
            parent.observed_at_s, self.compiled_at_s, policy.maximum_parent_age_s, "parent facts"
        )
        if context.airframe_profile_hash != parent.airframe_profile_sha256:
            raise ContinuationCompileError("current and parent airframe profile hash mismatch")
        if context.home_position != parent.home_position:
            raise ContinuationCompileError("current and parent HOME mismatch")
        start = context.aircraft_position
        for name, point in (("current aircraft", start), ("target", self.target)):
            if point.altitude_m is None or point.altitude_reference != "relative_home":
                raise ContinuationCompileError(f"{name} requires known relative_home altitude")
        if (round(start.latitude_deg * 1e7), round(start.longitude_deg * 1e7)) == (
            round(self.target.latitude_deg * 1e7),
            round(self.target.longitude_deg * 1e7),
        ):
            raise ContinuationCompileError("zero wire-rounded horizontal displacement")
        if _distance(start, self.target) <= 1.0e-9:
            raise ContinuationCompileError("zero physical horizontal displacement")
        # These are the original task/GPS/resource/energy rules, not a new
        # permissive trajectory schema. The full-sortie estimate is retained
        # as a conservative extra prerequisite, not claimed as segment wire.
        original = estimate_task(self.task, context, "multirotor", policy=policy.planner)
        validation_plan = plan_task(self.task, context, "multirotor", policy=policy.planner)
        validation_geometry = replace(geometry, waypoints=(start, self.target))
        _validate_plan_and_geometry(
            validation_plan,
            context,
            validation_geometry,
            safety,
            vehicle_label="multirotor",
            lifecycle_overhead_items=3,
        )
        # The original validator checks point centers. Also cover the loiter,
        # acceptance and GPS envelope, rather than granting a boundary orbit.
        for name, point, envelope in (
            ("current", start, context.gps_horizontal_accuracy_m),
            (
                "target",
                self.target,
                geometry.loiter_radius_m
                + geometry.acceptance_radius_m
                + context.gps_horizontal_accuracy_m,
            ),
        ):
            if _distance(safety.geofence_center, point) + envelope > safety.geofence_radius_m:
                raise ContinuationCompileError(f"{name} envelope exceeds geofence")
            if _distance(geometry.home, point) + envelope > safety.maximum_mission_radius_m:
                raise ContinuationCompileError(f"{name} envelope exceeds HOME radius")
        return_alt = max(
            float(start.altitude_m),
            float(self.target.altitude_m),
            policy.assumed_return_relative_altitude_m,
        )
        if (
            not safety.minimum_relative_altitude_m
            <= return_alt
            <= safety.maximum_relative_altitude_m
        ):
            raise ContinuationCompileError("assumed return altitude outside policy envelope")
        outbound, returning = (
            _distance(start, self.target),
            _distance(self.target, context.home_position)
            + _distance(context.home_position, geometry.landing),
        )
        outbound_s = _travel(start, self.target, context, policy.planner)
        return_s = _travel(self.target, context.home_position, context, policy.planner) + _travel(
            context.home_position, geometry.landing, context, policy.planner
        )
        buffer_m = (
            6.0 * context.gps_horizontal_accuracy_m
            + 2.0 * geometry.acceptance_radius_m
            + 2.0 * math.pi * geometry.loiter_radius_m
        )
        worst_progress = policy.planner.multirotor_cruise_speed_mps - math.hypot(
            context.wind_north_mps, context.wind_east_mps
        )
        if worst_progress <= policy.planner.minimum_speed_mps:
            raise ContinuationCompileError(
                "wind leaves insufficient conservative envelope progress"
            )
        buffer_s = buffer_m / worst_progress
        delta = float(self.target.altitude_m) - float(start.altitude_m)
        climb, descent = max(0.0, delta), max(0.0, -delta)
        return_climb = return_alt - float(self.target.altitude_m)
        return_descent = return_alt
        vertical_s = (climb + return_climb) / policy.assumed_climb_rate_mps + (
            descent + return_descent
        ) / policy.assumed_descent_rate_mps
        detection_s = sum(_DETECTION_OVERHEAD_S[action] for action in self.task.on_detection)
        duration = (
            outbound_s
            + return_s
            + buffer_s
            + vertical_s
            + geometry.loiter_time_s
            + policy.post_segment_wait_budget_s
            + policy.planner.landing_duration_s
            + detection_s
        ) * policy.planner.duration_safety_factor
        if not math.isfinite(duration) or duration > original.available_duration_s + 1e-9:
            raise ContinuationCompileError("continuation exceeds energy above return reserve")
        if duration > self.task.constraints.maximum_duration_s + 1e-9:
            raise ContinuationCompileError("continuation exceeds task maximum_duration_s")
        budget = ContinuationBudget(
            outbound,
            returning,
            buffer_m,
            outbound_s,
            return_s,
            buffer_s,
            climb,
            descent,
            return_climb,
            return_descent,
            vertical_s,
            geometry.loiter_time_s,
            policy.post_segment_wait_budget_s,
            policy.planner.landing_duration_s,
            detection_s,
            return_alt,
            policy.planner.duration_safety_factor,
            duration,
            original.available_duration_s,
            self.task.constraints.minimum_return_battery_pct,
            policy.planner.minimum_battery_margin_pct,
        )
        plan = PlatformNeutralPlan(
            plan_id=f"{self.task.task_id}:continuation-v1:{self.mission_id}",
            task_spec_hash=self.task.canonical_hash,
            context_snapshot_hash=context.canonical_hash,
            steps=(
                PlanStep(
                    0, PlanAction.TRANSIT, reference_id=self.source_intent_sha256, quantity=outbound
                ),
                PlanStep(
                    1,
                    validation_plan.steps[2].action,
                    reference_id="finite-continuation-loiter",
                    quantity=geometry.loiter_time_s,
                ),
            ),
        )
        items = tuple(
            MissionItem(
                sequence=sequence,
                command=command,
                frame=MissionFrame.GLOBAL_RELATIVE_ALT_INT,
                latitude_deg=point.latitude_deg,
                longitude_deg=point.longitude_deg,
                altitude_m=point.altitude_m,
                param1=geometry.loiter_time_s if sequence == 2 else 0.0,
                param2=geometry.acceptance_radius_m if sequence != 2 else 0.0,
                param3=geometry.loiter_radius_m if sequence == 2 else 0.0,
            )
            for sequence, command, point in (
                (0, MissionCommand.WAYPOINT, start),
                (1, MissionCommand.WAYPOINT, self.target),
                (2, MissionCommand.LOITER_TIME, self.target),
            )
        )
        mission = ExecutableMission(
            mission_id=self.mission_id,
            platform_plan_hash=plan.canonical_hash,
            airframe_profile_hash=parent.airframe_profile_sha256,
            airframe_adapter_id=CONTINUATION_ADAPTER_ID,
            airframe_adapter_hash=CONTINUATION_ADAPTER_HASH,
            items=items,
        )
        wire = compile_px4_mission_items(mission)
        for item in wire:
            altitude = struct.unpack("<f", struct.pack("<f", item.z))[0]
            if (
                not safety.minimum_relative_altitude_m
                <= altitude
                <= safety.maximum_relative_altitude_m
            ):
                raise ContinuationCompileError("wire-quantized altitude outside policy envelope")
        object.__setattr__(self, "plan", plan)
        object.__setattr__(self, "budget", budget)
        object.__setattr__(self, "original_task_estimate", original)
        object.__setattr__(
            self,
            "compiled_mission",
            CompiledPx4Mission(mission, wire),
        )

    @property
    def resume_sequence(self) -> int:
        return 1

    @property
    def wire_hash(self) -> str:
        return self.compiled_mission.wire_hash

    @property
    def executable_mission_hash(self) -> str:
        return self.compiled_mission.executable_mission.canonical_hash

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "contract": "multidetect.multirotor-continuation-compilation-v1",
            "task": self.task.to_mapping(),
            "current_context": self.current_context.to_mapping(),
            "target": self.target.to_mapping(),
            "runtime": strict_json_loads(self.runtime_json),
            "mission_id": self.mission_id,
            "parent": self.parent.to_mapping(),
            "parent_binding_sha256": self.parent.canonical_hash,
            "source_intent_sha256": self.source_intent_sha256,
            "compiled_at_s": self.compiled_at_s,
            "policy": self.policy.to_mapping(),
            "policy_sha256": self.policy.canonical_hash,
            "vehicle_class": self.vehicle_class,
            "plan": self.plan.to_mapping(),
            "budget": self.budget.to_mapping(),
            "original_task_estimate": self.original_task_estimate.to_mapping(),
            "compiled_mission": self.compiled_mission.to_mapping(),
            "executable_mission_hash": self.executable_mission_hash,
            "wire_hash": self.wire_hash,
            "resume_sequence": 1,
            "authority": {
                "transport_authorized": False,
                "flight_authorized": False,
                "airborne_facts_verified": False,
                "route_clearance_verified": False,
                "watchdog_implemented": False,
                "physical_calibration_verified": False,
                "gazebo_verified": False,
                "hardware_verified": False,
                "px4_execution_verified": False,
                "real_flight_verified": False,
            },
        }

    @classmethod
    def from_mapping(cls, raw: object) -> ContinuationCompilation:
        if not isinstance(raw, dict):
            raise ContinuationCompileError("ContinuationCompilation must be an object")
        result = cls(
            task=ResolvedTaskSpec.from_mapping(raw.get("task")),
            current_context=ContextSnapshot.from_mapping(raw.get("current_context")),
            target=GeoPoint.from_mapping(raw.get("target")),
            runtime_json=canonical_json(raw.get("runtime")),
            mission_id=raw.get("mission_id"),
            parent=ContinuationParentBinding.from_mapping(raw.get("parent")),
            source_intent_sha256=raw.get("source_intent_sha256"),
            compiled_at_s=raw.get("compiled_at_s"),
            policy=ContinuationCompilePolicy.from_mapping(raw.get("policy")),
            vehicle_class=raw.get("vehicle_class"),
        )
        if canonical_json(raw) != result.canonical_json:
            raise ContinuationCompileError("saved continuation differs from recomputed compilation")
        return result


def compile_multirotor_continuation(
    task: ResolvedTaskSpec,
    current_context: ContextSnapshot,
    target: GeoPoint,
    *,
    runtime: object,
    mission_id: str,
    parent: ContinuationParentBinding,
    now_s: float,
    source_intent_sha256: str,
    policy: ContinuationCompilePolicy | None = None,
    vehicle_class: str = "multirotor",
) -> ContinuationCompilation:
    """Author three canonical items; never call a transport or grant flight."""
    return ContinuationCompilation(
        task,
        current_context,
        target,
        canonical_json(runtime),
        mission_id,
        parent,
        source_intent_sha256,
        now_s,
        ContinuationCompilePolicy() if policy is None else policy,
        vehicle_class,
    )
