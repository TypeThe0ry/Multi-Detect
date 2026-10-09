"""Source-recomputed static SIM safety environment, never Actor observation.

The server SDF is a safety-supervisor input, not visual training truth.  This
adapter exposes an actual collision inventory and conservative surface bounds;
it does not set legacy map/DEM flags, qualify live freshness, prove contact or
grant execution.  Its loaded-world audit is rerun from original files on load
and revalidation, so a caller-supplied successful receipt is not sufficient.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .onboard_executed_trajectory import EvidenceBinding
from .serialization import AutonomyContractError, canonical_sha256, require_real
from .sim_loaded_world import audit_loaded_static_geometry
from .sim_static_route_clearance import (
    StaticCollision,
    StaticGazeboScene,
    inspect_static_gazebo_scene,
)

CONTRACT = "multidetect.static-teacher-SIM-environment-v1"
_TOLERANCE = 1e-9


@dataclass(frozen=True, slots=True)
class SurfaceHeightBound:
    east_m: float
    north_m: float
    ground_surface_up_m: float
    highest_static_surface_bound_up_m: float
    ground_collision_ids: tuple[str, ...]
    overhead_collision_ids: tuple[str, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "query_enu_m": [self.east_m, self.north_m],
            "ground_surface_up_m": self.ground_surface_up_m,
            "highest_static_surface_bound_up_m": self.highest_static_surface_bound_up_m,
            "ground_collision_ids": list(self.ground_collision_ids),
            "overhead_collision_ids": list(self.overhead_collision_ids),
            "height_kind": "source-derived-static-upper-bound-not-measured-DEM",
            "collision_contact_verified": False,
        }


def _plane_covers(collision: StaticCollision, east: float, north: float) -> bool:
    # A plane's declared dimensions bound the supported query domain.  Do not
    # extrapolate an engine's possibly infinite collision plane beyond them.
    delta = (east - collision.center_enu_m[0], north - collision.center_enu_m[1])
    rotation = collision.rotation
    local_east = rotation[0][0] * delta[0] + rotation[1][0] * delta[1]
    local_north = rotation[0][1] * delta[0] + rotation[1][1] * delta[1]
    return (
        abs(local_east) <= collision.half_extents_local_m[0] + _TOLERANCE
        and abs(local_north) <= collision.half_extents_local_m[1] + _TOLERANCE
    )


@dataclass(frozen=True, slots=True)
class StaticTeacherSimEnvironment:
    scene: StaticGazeboScene
    generated_sdf_source: EvidenceBinding
    loaded_geometry_audit_sha256: str

    def sample_surface_bound(self, east_m: float, north_m: float) -> SurfaceHeightBound:
        east = require_real(east_m, "east_m", minimum=-1e6, maximum=1e6)
        north = require_real(north_m, "north_m", minimum=-1e6, maximum=1e6)
        ground = tuple(
            c for c in self.scene.collisions if c.kind == "plane" and _plane_covers(c, east, north)
        )
        if not ground:
            raise AutonomyContractError("no verified ground surface covers the query")
        ground_up = max(c.center_enu_m[2] for c in ground)
        overhead = tuple(
            c
            for c in self.scene.collisions
            if c.kind != "plane"
            and c.minimum_enu_m[0] - _TOLERANCE <= east <= c.maximum_enu_m[0] + _TOLERANCE
            and c.minimum_enu_m[1] - _TOLERANCE <= north <= c.maximum_enu_m[1] + _TOLERANCE
            and c.maximum_enu_m[2] > ground_up
        )
        # AABBs intentionally overestimate rotated/curved surface heights.  A
        # clearance planner may use that conservative bound, not exact contact.
        upper = max(ground_up, *(c.maximum_enu_m[2] for c in overhead)) if overhead else ground_up
        if not math.isfinite(upper):
            raise AutonomyContractError("non-finite static surface bound")
        return SurfaceHeightBound(
            east,
            north,
            ground_up,
            upper,
            tuple(c.collision_id for c in ground),
            tuple(c.collision_id for c in overhead),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "contract": CONTRACT,
            "runtime_kind": "SIM",
            "scope": "static geometry safety supervisor only; not Actor input",
            "scene": self.scene.to_mapping(),
            "scene_sha256": self.scene.canonical_hash,
            "generated_sdf_source": self.generated_sdf_source.to_mapping(),
            "loaded_geometry_audit_sha256": self.loaded_geometry_audit_sha256,
            "capabilities": {
                "static_collision_inventory": True,
                "covered_ground_surface_queries": True,
                "conservative_static_surface_upper_bounds": True,
            },
            "claims": {
                name: False
                for name in (
                    "legacy_resource_flags_set",
                    "live_runtime_qualified",
                    "measured_DEM_available",
                    "dynamic_obstacles_covered",
                    "vehicle_envelope_qualified",
                    "contact_verified",
                    "whole_route_clearance_passed",
                    "operator_grant_issued",
                    "flight_executed",
                    "Actor_truth_input_allowed",
                    "physical_performance_claimed",
                    "final_release_allowed",
                )
            },
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    def revalidate_sources(self, artifact_root: str | Path) -> None:
        current = load_static_teacher_environment(
            artifact_root=artifact_root,
            generated_sdf_source=self.generated_sdf_source,
            world_source=self.scene.world_source,
            include_model_sources=dict(self.scene.include_sources),
        )
        if current != self:
            raise AutonomyContractError("static teacher environment changed during revalidation")


def load_static_teacher_environment(
    *,
    artifact_root: str | Path,
    generated_sdf_source: EvidenceBinding,
    world_source: EvidenceBinding,
    include_model_sources: Mapping[str, EvidenceBinding],
) -> StaticTeacherSimEnvironment:
    audit = audit_loaded_static_geometry(
        artifact_root=artifact_root,
        generated_sdf_source=generated_sdf_source,
        world_source=world_source,
        include_model_sources=include_model_sources,
    )
    if not audit.collision_definitions_match:
        raise AutonomyContractError(
            "actual loaded static geometry differs from declared safety map"
        )
    scene = inspect_static_gazebo_scene(
        artifact_root=artifact_root,
        world_source=world_source,
        include_model_sources=include_model_sources,
    )
    if not any(c.kind == "plane" for c in scene.collisions):
        raise AutonomyContractError("verified ground plane required for static surface queries")
    # Reopen after deriving the inventory: protect the multi-read derivation from
    # changes between the loaded-world comparison and scene construction.
    repeated = audit_loaded_static_geometry(
        artifact_root=artifact_root,
        generated_sdf_source=generated_sdf_source,
        world_source=world_source,
        include_model_sources=include_model_sources,
    )
    if repeated.canonical_hash != audit.canonical_hash:
        raise AutonomyContractError("loaded geometry source changed while building environment")
    return StaticTeacherSimEnvironment(scene, generated_sdf_source, audit.canonical_hash)
