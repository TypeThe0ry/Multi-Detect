"""Exact-scope v2 Mission compilation bindings, never transport permission.

This receipt records the scope in which a source-qualified context was checked.
It is not an issuer signature, a live Docker inspection, a ValidationReport or
an OperatorGrant. The caller must separately perform those checks before any
Mission transaction. Initially only the shared multirotor SIM compiler consumes
it; other vehicle/runtime domains remain fail closed pending their own inputs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields

from .contracts import ContextSnapshot, PlatformNeutralPlan
from .serialization import (
    canonical_sha256,
    require_real,
    require_schema_version,
    require_sha256,
    require_text,
    strict_object,
)

CONTRACT = "multidetect.context-v2-mission-compilation-admission-v1"
MAXIMUM_ADMISSION_AGE_S = 2.0


@dataclass(frozen=True, slots=True)
class ContextV2MissionAdmission:
    context_snapshot_sha256: str
    task_spec_sha256: str
    geometry_sha256: str
    safety_policy_sha256: str
    airframe_profile_id: str
    airframe_profile_sha256: str
    runtime_attestation_sha256: str
    planning_qualification_sha256: str
    runtime_kind: str
    vehicle_class: str
    clock_id: str
    observed_at_s: float
    qualified_at_s: float
    expires_at_s: float
    schema_version: int = 1

    def __post_init__(self) -> None:
        require_schema_version(self.schema_version)
        for member in fields(self):
            name = member.name
            if name.endswith("sha256"):
                object.__setattr__(self, name, require_sha256(getattr(self, name), name))
        for name in ("airframe_profile_id", "clock_id"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        if self.runtime_kind != "SIM" or self.vehicle_class != "multirotor":
            raise ValueError("v2 Mission compilation admission supports multirotor SIM only")
        for name in ("observed_at_s", "qualified_at_s", "expires_at_s"):
            object.__setattr__(self, name, require_real(getattr(self, name), name, minimum=0.0))
        if not self.observed_at_s <= self.qualified_at_s < self.expires_at_s:
            raise ValueError("Mission admission times are not ordered")
        if self.expires_at_s - self.observed_at_s > MAXIMUM_ADMISSION_AGE_S:
            raise ValueError("Mission admission exceeds the maximum original-observation age")

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    def to_mapping(self) -> dict[str, object]:
        return {"contract": CONTRACT, **asdict(self)}

    @classmethod
    def from_mapping(cls, raw: object) -> ContextV2MissionAdmission:
        value = strict_object(
            raw,
            name="ContextV2MissionAdmission",
            required=frozenset({"contract", *(member.name for member in fields(cls))}),
        )
        if value["contract"] != CONTRACT:
            raise ValueError("unsupported v2 Mission compilation admission contract")
        return cls(**{name: value[name] for name in value if name != "contract"})

    def validate_for(
        self,
        *,
        context: ContextSnapshot,
        plan: PlatformNeutralPlan,
        geometry_mapping: object,
        safety_policy_mapping: object,
        airframe_profile_sha256: str,
        vehicle_class: str,
        now_s: float,
        clock_id: str,
    ) -> None:
        """Recheck exact bindings and age, without granting execution authority."""

        current = require_real(now_s, "admission_now_s", minimum=0.0)
        if require_text(clock_id, "admission_clock_id") != self.clock_id:
            raise ValueError("Mission admission clock domain mismatch")
        if not self.qualified_at_s <= current < self.expires_at_s:
            raise ValueError("Mission admission is future-dated or expired")
        if context.schema_version != 2 or context.provenance is None:
            raise ValueError("Mission admission requires a v2 context")
        if context.provenance.runtime_kind != self.runtime_kind:
            raise ValueError("Mission admission runtime domain mismatch")
        if (
            context.provenance.endurance.kind == "explicit_SIM_energy_model"
            and current != self.qualified_at_s
        ):
            raise ValueError("modeled budget must be requalified at the current compilation time")
        if context.captured_at_s != self.observed_at_s:
            raise ValueError("Mission admission must retain the original observation time")
        if (
            context.canonical_hash != self.context_snapshot_sha256
            or plan.context_snapshot_hash != self.context_snapshot_sha256
            or plan.task_spec_hash != self.task_spec_sha256
            or canonical_sha256(geometry_mapping) != self.geometry_sha256
            or canonical_sha256(safety_policy_mapping) != self.safety_policy_sha256
        ):
            raise ValueError("Mission admission context/task/geometry/policy binding mismatch")
        profile = require_sha256(airframe_profile_sha256, "airframe_profile_sha256")
        if (
            context.airframe_profile_id != self.airframe_profile_id
            or context.airframe_profile_hash != self.airframe_profile_sha256
            or profile != self.airframe_profile_sha256
            or vehicle_class != self.vehicle_class
        ):
            raise ValueError("Mission admission vehicle or airframe profile mismatch")
        # Keep provenance/capability validation in the shared planner, not a
        # second weaker copy. A hash-bound source declaration is not permission.
        from .planner import _validate_v2_planning_sources

        _validate_v2_planning_sources(context, vehicle_class)
