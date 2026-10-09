"""Experimental held/yaw Mission authoring, never a flight or teacher grant.

One shared multirotor TAKEOFF -> WAYPOINT(s) -> LOITER_TIME -> LAND is authored.
Only WAYPOINT param1 (hold seconds) and param4 (clockwise yaw degrees from
north) differ from the frozen compiler. MAVLink NAV_WAYPOINT and the official
PX4 v1.16 mission implementation define those fields; actual firmware support,
readback, yaw attainment and settled endpoints still require runtime evidence.

The shared planner does not budget these holds or 3D/yaw motion. An explicit
supplement below is added to its estimate and rechecked against the same task
and battery reserve. Its rates are conservative planning assumptions, not
measured aircraft performance. No planned waypoint/yaw becomes an observed
pose, expert label, runtime Actor input, OperatorGrant or execution authority.
The caller must separately bind the authoring hash AND exact executable/wire
hashes to real validation/intent documents and the persistent transaction.
Do not use the acceptance fixture's automatically generated grant as approval.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace

from ..geolocation_accuracy import wgs84_distance_m
from ..mission_planning import PlannedMissionCompilation
from ..px4_mission import (
    CompiledPx4Mission,
    MultirotorMissionGeometry,
    Px4MultirotorSafetyPolicy,
    compile_multirotor_executable_mission,
    compile_px4_mission_items,
)
from .contracts import (
    ContextSnapshot,
    GeoPoint,
    MissionCommand,
    PlanAction,
    ResolvedTaskSpec,
    TaskType,
)
from .mission_context_admission import ContextV2MissionAdmission
from .planner import PlannerPolicy, RouteAcquisitionRequirements, estimate_task, plan_task
from .serialization import (
    canonical_json,
    canonical_sha256,
    require_array,
    require_int,
    require_real,
    require_schema_version,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)

CONTRACT = "multidetect.onboard-teacher-held-mission-v1"
CONTRACT_V2 = "multidetect.onboard-teacher-held-mission-v2"
MAXIMUM_LEGS = 16
MAXIMUM_LEG_HOLD_S = 60.0
MINIMUM_ACCEPTANCE_RADIUS_M = 0.05
MAXIMUM_ACCEPTANCE_RADIUS_M = 1.0
VERTICAL_BUDGET_SPEED_MPS = 0.5
YAW_BUDGET_RATE_DEG_S = 5.0


def _rules(version: int = 1) -> dict[str, object]:
    result = {
        "maximum_legs": MAXIMUM_LEGS,
        "maximum_leg_hold_s": MAXIMUM_LEG_HOLD_S,
        "acceptance_radius_range_m": [MINIMUM_ACCEPTANCE_RADIUS_M, MAXIMUM_ACCEPTANCE_RADIUS_M],
        "vertical_budget_speed_mps": VERTICAL_BUDGET_SPEED_MPS,
        "yaw_budget_rate_deg_s": YAW_BUDGET_RATE_DEG_S,
        "yaw_budget_degrees_per_leg": 360.0,
        "supplemental_horizontal_speed": "planner_policy.minimum_speed_mps",
        "shared_plan_does_not_include_holds": True,
        "transport_adapter": "unchanged-shared-multirotor",
    }
    if version == 2:
        result.update(
            explicit_SIM_endurance_age="full elapsed time; fixed sampled-battery reserve",
            compilation_admission="exact-scoped multirotor SIM; never an execution grant",
        )
    return result


@dataclass(frozen=True, slots=True)
class HeldTeacherLeg:
    """A planned WAYPOINT command, NOT an executed displacement or STOP label."""

    sequence: int
    hold_s: float
    yaw_deg: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "sequence",
            require_int(self.sequence, "sequence", minimum=1, maximum=MAXIMUM_LEGS),
        )
        object.__setattr__(
            self,
            "hold_s",
            require_real(self.hold_s, "hold_s", minimum=0.0, maximum=MAXIMUM_LEG_HOLD_S),
        )
        yaw = require_real(self.yaw_deg, "yaw_deg", minimum=-3600.0, maximum=3600.0)
        object.__setattr__(self, "yaw_deg", yaw % 360.0 + 0.0)

    @classmethod
    def from_mapping(cls, raw: object) -> HeldTeacherLeg:
        value = strict_object(
            raw, name="HeldTeacherLeg", required=frozenset({"sequence", "hold_s", "yaw_deg"})
        )
        return cls(value["sequence"], value["hold_s"], value["yaw_deg"])

    def to_mapping(self) -> dict[str, object]:
        return {"sequence": self.sequence, "hold_s": self.hold_s, "yaw_deg": self.yaw_deg}


def _distance(left: GeoPoint, right: GeoPoint) -> float:
    return wgs84_distance_m(
        latitude1_deg=left.latitude_deg,
        longitude1_deg=left.longitude_deg,
        latitude2_deg=right.latitude_deg,
        longitude2_deg=right.longitude_deg,
    )


@dataclass(frozen=True, slots=True)
class TeacherMissionDraft:
    """Recompilable command artifact; a successful structural check is not approval.

    ``compilation`` fits the existing shared types. The generic plan retains
    its original meaning; ``authoring_hash`` additionally binds ALL held/yaw
    inputs, policies and supplemental budget rules. Source authenticity,
    current-context freshness, issuer authority and real feedback are external.
    """

    task: ResolvedTaskSpec
    context: ContextSnapshot
    geometry: MultirotorMissionGeometry
    safety_policy: Px4MultirotorSafetyPolicy
    legs: tuple[HeldTeacherLeg, ...]
    airframe_profile_hash: str
    planner_policy: PlannerPolicy = field(default_factory=PlannerPolicy)
    mission_id: str | None = None
    schema_version: int = 1
    context_admission: ContextV2MissionAdmission | None = None
    admission_now_s: float | None = None
    admission_clock_id: str | None = None
    _compilation: PlannedMissionCompilation = field(init=False, repr=False)
    _budget: tuple[tuple[str, float], ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        for name, kind in (
            ("task", ResolvedTaskSpec),
            ("context", ContextSnapshot),
            ("geometry", MultirotorMissionGeometry),
            ("safety_policy", Px4MultirotorSafetyPolicy),
            ("planner_policy", PlannerPolicy),
        ):
            if not isinstance(getattr(self, name), kind):
                raise TypeError(f"{name} must be {kind.__name__}")
        require_schema_version(
            self.schema_version, expected=2 if self.context.schema_version == 2 else 1
        )
        requirements = None
        if self.schema_version == 2:
            if not isinstance(self.context_admission, ContextV2MissionAdmission):
                raise ValueError(
                    "v2 teacher authoring requires scoped Mission compilation admission"
                )
            requirements = RouteAcquisitionRequirements(
                self.task.canonical_hash,
                self.context.canonical_hash,
                modeled_endurance_receive_age_s=(
                    self.context_admission.qualified_at_s - self.context_admission.observed_at_s
                    if self.context.provenance.endurance.kind == "explicit_SIM_energy_model"
                    else 0.0
                ),
            )
        elif any(
            value is not None
            for value in (self.context_admission, self.admission_now_s, self.admission_clock_id)
        ):
            raise ValueError("v1 teacher authoring must not consume v2 admission inputs")
        if not isinstance(self.legs, tuple) or not all(
            isinstance(leg, HeldTeacherLeg) for leg in self.legs
        ):
            raise TypeError("legs must be a tuple of HeldTeacherLeg")
        if not 2 <= len(self.legs) <= MAXIMUM_LEGS:
            raise ValueError("teacher route must contain 2..16 held leg commands")
        if tuple(leg.sequence for leg in self.legs) != tuple(range(1, len(self.legs) + 1)):
            raise ValueError("held leg sequences must match contiguous WAYPOINT order")
        if len(self.legs) != len(self.geometry.waypoints):
            raise ValueError("one held leg command is required per geometry waypoint")
        if self.task.geometry.centerline != self.geometry.waypoints:
            raise ValueError("task centerline must exactly match authored waypoint geometry")
        if self.task.task_type is not TaskType.SURVEY or self.task.on_detection:
            raise ValueError("teacher route requires SURVEY with no detection branches")
        if self.task.completion_action is not PlanAction.RETURN:
            raise ValueError("teacher route must explicitly RETURN before LAND")
        if not self.task.constraints.remain_inside_geofence:
            raise ValueError("teacher route requires remain_inside_geofence")
        if (
            self.geometry.landing.latitude_deg,
            self.geometry.landing.longitude_deg,
        ) != (self.geometry.home.latitude_deg, self.geometry.home.longitude_deg):
            raise ValueError("teacher RETURN landing must exactly match authored home coordinates")
        profile_hash = require_sha256(self.airframe_profile_hash, "airframe_profile_hash")
        object.__setattr__(self, "airframe_profile_hash", profile_hash)
        if self.context.airframe_profile_hash not in (None, profile_hash):
            raise ValueError("airframe_profile_hash does not match context")
        object.__setattr__(
            self,
            "mission_id",
            require_text(
                self.mission_id
                if self.mission_id is not None
                else f"{self.task.task_id}:held-v{self.schema_version}",
                "mission_id",
            ),
        )
        radius = require_real(
            self.geometry.acceptance_radius_m,
            "acceptance_radius_m",
            minimum=MINIMUM_ACCEPTANCE_RADIUS_M,
            maximum=MAXIMUM_ACCEPTANCE_RADIUS_M,
        )
        baseline = estimate_task(
            self.task,
            self.context,
            "multirotor",
            policy=self.planner_policy,
            resource_requirements=requirements,
        )
        plan = plan_task(
            self.task,
            self.context,
            "multirotor",
            policy=self.planner_policy,
            resource_requirements=requirements,
        )
        original = compile_multirotor_executable_mission(
            platform_plan=plan,
            context_snapshot=self.context,
            geometry=self.geometry,
            policy=self.safety_policy,
            airframe_profile_hash=profile_hash,
            mission_id=self.mission_id,
            context_admission=self.context_admission,
            admission_now_s=self.admission_now_s,
            admission_clock_id=self.admission_clock_id,
        )
        # Do not use an acceptance sphere extending outside the authored safety envelope.
        for point in self.geometry.waypoints:
            if _distance(point, self.safety_policy.geofence_center) + radius > (
                self.safety_policy.geofence_radius_m
            ):
                raise ValueError("waypoint acceptance envelope extends outside geofence")
            if _distance(point, self.geometry.home) + radius > (
                self.safety_policy.maximum_mission_radius_m
            ):
                raise ValueError("waypoint acceptance envelope exceeds home radius")
            if not self.safety_policy.minimum_relative_altitude_m <= point.altitude_m - radius:
                raise ValueError("waypoint acceptance envelope falls below minimum altitude")
            if point.altitude_m + radius > self.safety_policy.maximum_relative_altitude_m:
                raise ValueError("waypoint acceptance envelope exceeds maximum altitude")
        dwell = sum(leg.hold_s for leg in self.legs) + self.geometry.loiter_time_s
        if dwell > self.safety_policy.maximum_loiter_time_s:
            raise ValueError("total waypoint holds plus terminal loiter exceed dwell policy limit")
        # Deliberately add, not replace, the shared estimate: no refund of its lifecycle budget.
        points = (self.geometry.home, *self.geometry.waypoints, self.geometry.landing)
        horizontal = sum(_distance(a, b) for a, b in zip(points[:-1], points[1:], strict=True))
        altitudes = (
            0.0,
            self.geometry.takeoff_altitude_m,
            *(point.altitude_m for point in self.geometry.waypoints),
            0.0,
        )
        vertical = sum(abs(a - b) for a, b in zip(altitudes[:-1], altitudes[1:], strict=True))
        travel = horizontal / self.planner_policy.minimum_speed_mps
        vertical_s = vertical / VERTICAL_BUDGET_SPEED_MPS
        yaw_s = len(self.legs) * 360.0 / YAW_BUDGET_RATE_DEG_S
        supplement = (dwell + travel + vertical_s + yaw_s) * (
            self.planner_policy.duration_safety_factor
        )
        duration = baseline.estimated_duration_s + supplement
        require_real(duration, "total_estimated_duration_s", minimum=0.0)
        if duration > baseline.available_duration_s:
            raise ValueError("held/yaw supplemental duration exceeds energy budget above reserve")
        if duration > self.task.constraints.maximum_duration_s:
            raise ValueError("held/yaw supplemental duration exceeds task maximum_duration_s")
        items = list(original.executable_mission.items)
        for leg in self.legs:
            if items[leg.sequence].command is not MissionCommand.WAYPOINT:
                raise ValueError("held command does not address a WAYPOINT")
            items[leg.sequence] = replace(
                items[leg.sequence], param1=leg.hold_s, param4=leg.yaw_deg
            )
        mission = replace(original.executable_mission, items=tuple(items))
        compiled = CompiledPx4Mission(mission, compile_px4_mission_items(mission))
        object.__setattr__(
            self,
            "_compilation",
            PlannedMissionCompilation(
                "multirotor", replace(baseline, estimated_duration_s=duration), plan, compiled
            ),
        )
        object.__setattr__(
            self,
            "_budget",
            tuple(
                {
                    "shared_estimated_duration_s": baseline.estimated_duration_s,
                    "total_dwell_s": dwell,
                    "supplemental_horizontal_distance_m": horizontal,
                    "supplemental_horizontal_time_s": travel,
                    "supplemental_vertical_distance_m": vertical,
                    "supplemental_vertical_time_s": vertical_s,
                    "supplemental_yaw_time_s": yaw_s,
                    "supplemental_duration_s": supplement,
                    "total_estimated_duration_s": duration,
                    "available_duration_s": baseline.available_duration_s,
                    "task_maximum_duration_s": self.task.constraints.maximum_duration_s,
                }.items()
            ),
        )

    @property
    def compilation(self) -> PlannedMissionCompilation:
        return self._compilation

    @property
    def compiled_mission(self) -> CompiledPx4Mission:
        return self.compilation.compiled_mission

    @property
    def budget_review(self) -> dict[str, float]:
        return dict(self._budget)

    @property
    def validation_checks(self) -> dict[str, object]:
        return {
            "shared_readiness_geometry_safety_checks_passed": True,
            "supplemental_duration_reserve_check_passed": True,
            "held_yaw_wire_semantics_checked": True,
            "scope": "structural authoring only; source/current-runtime authenticity not verified",
            "planned_hold_is_not_stop_complete_or_rtl": True,
        }

    def _inputs(self) -> dict[str, object]:
        result = {
            "task": self.task.to_mapping(),
            "context": self.context.to_mapping(),
            "geometry": self.geometry.to_mapping(),
            "safety_policy": self.safety_policy.to_mapping(),
            "legs": [leg.to_mapping() for leg in self.legs],
            "airframe_profile_hash": self.airframe_profile_hash,
            "planner_policy": asdict(self.planner_policy),
            "mission_id": self.mission_id,
        }
        if self.schema_version == 2:
            assert self.context_admission is not None
            result.update(
                context_admission=self.context_admission.to_mapping(),
                admission_now_s=self.admission_now_s,
                admission_clock_id=self.admission_clock_id,
            )
        return result

    @property
    def contract(self) -> str:
        return CONTRACT_V2 if self.schema_version == 2 else CONTRACT

    @property
    def authoring_hash(self) -> str:
        return canonical_sha256(
            {
                "contract": self.contract,
                "inputs": self._inputs(),
                "authoring_rules": _rules(self.schema_version),
            }
        )

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    def to_mapping(self) -> dict[str, object]:
        return {
            "contract": self.contract,
            "schema_version": self.schema_version,
            "inputs": self._inputs(),
            "authoring_rules": _rules(self.schema_version),
            "authoring_hash": self.authoring_hash,
            "compilation": self.compilation.to_mapping(),
            "budget_review": self.budget_review,
            "validation_checks": self.validation_checks,
            "claims": {
                name: False
                for name in (
                    "provenance_verified",
                    "current_context_freshness_verified",
                    "firmware_semantics_verified_on_actual_vehicle",
                    "mission_approved",
                    "mission_submitted",
                    "mission_executed",
                    "observed_endpoints_verified",
                    "actual_yaw_or_hold_verified",
                    "teacher_expert_verified",
                    "train_admitted",
                    "quality_claim",
                    "default_policy_modified",
                    "execution_authority",
                    "physical_output_enabled",
                    "final_release_allowed",
                )
            },
        }

    @classmethod
    def from_mapping(cls, raw: object) -> TeacherMissionDraft:
        """Verify frozen authoring bytes, not the current runtime's live freshness."""
        value = strict_object(
            raw,
            name="TeacherMissionDraft",
            required=frozenset(
                {
                    "contract",
                    "schema_version",
                    "inputs",
                    "authoring_rules",
                    "authoring_hash",
                    "compilation",
                    "budget_review",
                    "validation_checks",
                    "claims",
                }
            ),
        )
        version = require_int(value["schema_version"], "schema_version", minimum=1, maximum=2)
        if value["contract"] != (CONTRACT_V2 if version == 2 else CONTRACT):
            raise ValueError("unsupported teacher authoring contract")
        inputs = strict_object(
            value["inputs"],
            name="TeacherMissionDraft.inputs",
            required=frozenset(
                {
                    "task",
                    "context",
                    "geometry",
                    "safety_policy",
                    "legs",
                    "airframe_profile_hash",
                    "planner_policy",
                    "mission_id",
                }
            )
            | (
                frozenset({"context_admission", "admission_now_s", "admission_clock_id"})
                if version == 2
                else frozenset()
            ),
        )
        planner = strict_object(
            inputs["planner_policy"],
            name="PlannerPolicy",
            required=frozenset(f.name for f in fields(PlannerPolicy)),
        )
        result = cls(
            task=ResolvedTaskSpec.from_mapping(inputs["task"]),
            context=ContextSnapshot.from_mapping(inputs["context"]),
            geometry=MultirotorMissionGeometry.from_mapping(inputs["geometry"]),
            safety_policy=Px4MultirotorSafetyPolicy.from_mapping(inputs["safety_policy"]),
            legs=tuple(
                HeldTeacherLeg.from_mapping(leg) for leg in require_array(inputs["legs"], "legs")
            ),
            airframe_profile_hash=inputs["airframe_profile_hash"],
            planner_policy=PlannerPolicy(**planner),
            mission_id=inputs["mission_id"],
            schema_version=version,
            context_admission=(
                ContextV2MissionAdmission.from_mapping(inputs["context_admission"])
                if version == 2
                else None
            ),
            admission_now_s=inputs.get("admission_now_s"),
            admission_clock_id=inputs.get("admission_clock_id"),
        )
        if canonical_json(value) != canonical_json(result.to_mapping()):
            raise ValueError("teacher authoring artifact does not match exact recompiled semantics")
        return result

    @classmethod
    def from_json(cls, payload: str | bytes | bytearray) -> TeacherMissionDraft:
        return cls.from_mapping(strict_json_loads(payload, name="TeacherMissionDraft"))


def compile_teacher_held_mission(
    *,
    task: ResolvedTaskSpec,
    context: ContextSnapshot,
    geometry: MultirotorMissionGeometry,
    safety_policy: Px4MultirotorSafetyPolicy,
    legs: tuple[HeldTeacherLeg, ...],
    airframe_profile_hash: str,
    planner_policy: PlannerPolicy | None = None,
    mission_id: str | None = None,
    context_admission: ContextV2MissionAdmission | None = None,
    admission_now_s: float | None = None,
    admission_clock_id: str | None = None,
) -> TeacherMissionDraft:
    """Author only. No I/O, transport, automatic approval, Actor or execution."""

    return TeacherMissionDraft(
        task,
        context,
        geometry,
        safety_policy,
        legs,
        airframe_profile_hash,
        PlannerPolicy() if planner_policy is None else planner_policy,
        mission_id,
        schema_version=2 if context.schema_version == 2 else 1,
        context_admission=context_admission,
        admission_now_s=admission_now_s,
        admission_clock_id=admission_clock_id,
    )
