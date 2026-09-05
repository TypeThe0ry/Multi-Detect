"""Join the shared task planner to the canonical PX4 mission compiler.

The task planner deliberately stops at a platform-neutral plan.  This module
is the small, typed seam used by authoring tools and deterministic SIM/HIL
fixtures to pass that exact plan through :func:`compile_executable_mission`.
It does not open a transport, upload a mission or enable flight-control
output; those operations remain owned by the mission transaction/runtime
layers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .autonomy import (
    ContextSnapshot,
    ExecutableMission,
    PlannerPolicy,
    PlanningEstimate,
    PlatformNeutralPlan,
    ResolvedTaskSpec,
    estimate_task,
    normalize_vehicle_class,
    plan_task,
)
from .autonomy.serialization import (
    require_schema_version,
    require_sha256,
    strict_json_loads,
    strict_object,
)
from .px4_mission import (
    CompiledPx4Mission,
    FixedWingMissionGeometry,
    MultirotorMissionGeometry,
    Px4FixedWingSafetyPolicy,
    Px4MissionSafetyPolicy,
    Px4MultirotorSafetyPolicy,
    VtolMissionGeometry,
    compile_executable_mission,
)

MissionGeometry = VtolMissionGeometry | FixedWingMissionGeometry | MultirotorMissionGeometry
MissionPolicy = Px4MissionSafetyPolicy | Px4FixedWingSafetyPolicy | Px4MultirotorSafetyPolicy

PLANNED_MISSION_COMPILATION_SCHEMA_VERSION = 1
MISSION_COMPILE_RUNTIME_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class PlannedMissionCompilation:
    """The three canonical artifacts produced by one planning/compile pass."""

    vehicle_class: str
    estimate: PlanningEstimate
    plan: PlatformNeutralPlan
    compiled_mission: CompiledPx4Mission
    schema_version: int = PLANNED_MISSION_COMPILATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "schema_version",
            require_schema_version(
                self.schema_version,
                expected=PLANNED_MISSION_COMPILATION_SCHEMA_VERSION,
            ),
        )
        normalized = normalize_vehicle_class(self.vehicle_class)
        object.__setattr__(self, "vehicle_class", normalized)
        if not isinstance(self.estimate, PlanningEstimate):
            raise TypeError("estimate must be a PlanningEstimate")
        if not isinstance(self.plan, PlatformNeutralPlan):
            raise TypeError("plan must be a PlatformNeutralPlan")
        if not isinstance(self.compiled_mission, CompiledPx4Mission):
            raise TypeError("compiled_mission must be a CompiledPx4Mission")
        if self.estimate.vehicle_class != normalized:
            raise ValueError("estimate vehicle_class does not match compilation")
        if self.plan.plan_id != f"{self.estimate.task_id}:platform-neutral-v1":
            raise ValueError("estimate task_id does not match platform plan")
        if self.compiled_mission.executable_mission.platform_plan_hash != self.plan.canonical_hash:
            raise ValueError("compiled mission is not bound to the platform plan")

    @property
    def executable_mission_hash(self) -> str:
        """Canonical hash that the mission transaction must bind."""

        return self.compiled_mission.executable_mission.canonical_hash

    @property
    def wire_hash(self) -> str:
        """Canonical hash of the MISSION_ITEM_INT wire sequence."""

        return self.compiled_mission.wire_hash

    @classmethod
    def from_mapping(cls, raw: object) -> PlannedMissionCompilation:
        """Re-open a saved plan/mission join with strict hash verification."""

        value = strict_object(
            raw,
            name="PlannedMissionCompilation",
            required=frozenset(
                {
                    "schema_version",
                    "vehicle_class",
                    "estimate",
                    "plan",
                    "compiled_mission",
                    "executable_mission_hash",
                    "wire_hash",
                }
            ),
        )
        result = cls(
            vehicle_class=value["vehicle_class"],
            estimate=PlanningEstimate.from_mapping(value["estimate"]),
            plan=PlatformNeutralPlan.from_mapping(value["plan"]),
            compiled_mission=CompiledPx4Mission.from_mapping(value["compiled_mission"]),
            schema_version=require_schema_version(
                value["schema_version"],
                expected=PLANNED_MISSION_COMPILATION_SCHEMA_VERSION,
            ),
        )
        executable_hash = require_sha256(
            value["executable_mission_hash"], "executable_mission_hash"
        )
        if executable_hash != result.executable_mission_hash:
            raise ValueError("executable_mission_hash does not match compiled mission")
        wire_hash = require_sha256(value["wire_hash"], "wire_hash")
        if wire_hash != result.wire_hash:
            raise ValueError("wire_hash does not match compiled mission")
        return result

    def to_mapping(self) -> dict[str, object]:
        """Return a hash-bound, JSON-compatible planning/compile artifact."""

        return {
            "schema_version": self.schema_version,
            "vehicle_class": self.vehicle_class,
            "estimate": self.estimate.to_mapping(),
            "plan": self.plan.to_mapping(),
            "compiled_mission": self.compiled_mission.to_mapping(),
            "executable_mission_hash": self.executable_mission_hash,
            "wire_hash": self.wire_hash,
        }


def compile_planned_task_mission(
    task: ResolvedTaskSpec,
    context: ContextSnapshot,
    vehicle_class: str,
    *,
    geometry: MissionGeometry,
    policy: MissionPolicy,
    airframe_profile_hash: str,
    mission_id: str | None = None,
    planner_policy: PlannerPolicy | None = None,
) -> PlannedMissionCompilation:
    """Plan and compile one task through the shared PX4 mission path.

    ``geometry`` and ``policy`` are runtime-channel inputs for the selected
    airframe.  The task/context validation and platform-neutral plan are
    always produced first, so SIM, HIL and REAL authoring can compare the same
    plan/hash before selecting the vehicle adapter.
    """

    estimate = estimate_task(task, context, vehicle_class, policy=planner_policy)
    plan = plan_task(task, context, vehicle_class, policy=planner_policy)
    normalized_vehicle = normalize_vehicle_class(vehicle_class)
    compiled = compile_executable_mission(
        vehicle_class=normalized_vehicle,
        platform_plan=plan,
        context_snapshot=context,
        geometry=geometry,
        policy=policy,
        airframe_profile_hash=airframe_profile_hash,
        mission_id=mission_id,
    )
    return PlannedMissionCompilation(
        vehicle_class=normalized_vehicle,
        estimate=estimate,
        plan=plan,
        compiled_mission=compiled,
    )


def parse_mission_compile_runtime(
    raw: object,
    vehicle_class: str,
) -> tuple[MissionGeometry, MissionPolicy]:
    """Parse one strict geometry/policy document for the compile seam.

    The runtime document contains no task truth or evaluator expectations.  It
    is deliberately a small adapter input so the CLI, SIM fixture and future
    HIL/REAL authoring tools all instantiate the same concrete geometry and
    safety-policy types before entering :func:`compile_planned_task_mission`.
    """

    value = strict_object(
        raw,
        name="MissionCompileRuntime",
        required=frozenset(
            {"schema_version", "home_position", "mission_geometry", "safety_policy"}
        ),
        optional=frozenset({"vehicle_class"}),
    )
    require_schema_version(
        value["schema_version"], expected=MISSION_COMPILE_RUNTIME_SCHEMA_VERSION
    )
    normalized = normalize_vehicle_class(vehicle_class)
    if "vehicle_class" in value and normalize_vehicle_class(value["vehicle_class"]) != normalized:
        raise ValueError("runtime vehicle_class does not match requested vehicle_class")
    geometry_input = {
        "home_position": value["home_position"],
        "mission_geometry": value["mission_geometry"],
    }
    if normalized == "vtol":
        geometry: MissionGeometry = VtolMissionGeometry.from_mapping(geometry_input)
        policy: MissionPolicy = Px4MissionSafetyPolicy.from_mapping(value["safety_policy"])
    elif normalized == "fixed-wing":
        geometry = FixedWingMissionGeometry.from_mapping(geometry_input)
        policy = Px4FixedWingSafetyPolicy.from_mapping(value["safety_policy"])
    else:
        geometry = MultirotorMissionGeometry.from_mapping(geometry_input)
        policy = Px4MultirotorSafetyPolicy.from_mapping(value["safety_policy"])
    return geometry, policy


def compile_planned_task_mission_from_runtime(
    task: ResolvedTaskSpec,
    context: ContextSnapshot,
    vehicle_class: str,
    *,
    runtime: object,
    airframe_profile_hash: str,
    mission_id: str | None = None,
    planner_policy: PlannerPolicy | None = None,
) -> PlannedMissionCompilation:
    """Compile strict task/context/runtime JSON through the shared seam."""

    geometry, policy = parse_mission_compile_runtime(runtime, vehicle_class)
    return compile_planned_task_mission(
        task,
        context,
        vehicle_class,
        geometry=geometry,
        policy=policy,
        airframe_profile_hash=airframe_profile_hash,
        mission_id=mission_id,
        planner_policy=planner_policy,
    )


def load_executable_mission_document(path: str | Path) -> ExecutableMission:
    """Load a bare, compiled, or planner-joined Mission artifact strictly."""

    source = Path(path).expanduser()
    raw = strict_json_loads(source.read_bytes(), name="Mission document")
    if not isinstance(raw, Mapping):
        raise ValueError("Mission document must be a JSON object")
    if "compiled_mission" in raw:
        return PlannedMissionCompilation.from_mapping(raw).compiled_mission.executable_mission
    if "executable_mission" in raw:
        return CompiledPx4Mission.from_mapping(raw).executable_mission
    return ExecutableMission.from_mapping(raw)


__all__ = [
    "MissionGeometry",
    "MissionPolicy",
    "MISSION_COMPILE_RUNTIME_SCHEMA_VERSION",
    "PLANNED_MISSION_COMPILATION_SCHEMA_VERSION",
    "PlannedMissionCompilation",
    "compile_planned_task_mission",
    "compile_planned_task_mission_from_runtime",
    "load_executable_mission_document",
    "parse_mission_compile_runtime",
]
