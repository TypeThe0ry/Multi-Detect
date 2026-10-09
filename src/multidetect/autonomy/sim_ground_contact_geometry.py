"""Signed, source-recomputed SIM contact geometry; never live contact authority.

The fixed one-micrometre negative allowance is numerical geometry policy, not
an ODE setting or force observation. The positive one-millimetre allowance
does not permit a detached vehicle. All-rotation footprint containment and a
small explicit saved-pose tilt domain remain independent requirements.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from .onboard_executed_trajectory import EvidenceBinding
from .serialization import (
    AutonomyContractError,
    canonical_json,
    canonical_sha256,
    require_real,
    strict_json_loads,
)
from .sim_airframe_envelope import (
    MAXIMUM_SOURCE_BYTES,
    _loaded,
    _models,
    audit_x500_collision_envelope,
)
from .sim_static_route_clearance import (
    _IDENTITY,
    _bound_bytes,
    _mv,
    _pose,
)
from .sim_teacher_environment import (
    StaticTeacherSimEnvironment,
    load_static_teacher_environment,
)

CONTRACT = "multidetect.signed-source-derived-SIM-ground-contact-geometry-v1"
NUMERICAL_PENETRATION_ALLOWANCE_M = 1e-6
POSITIVE_GAP_ALLOWANCE_M = 1e-3


def _require(condition, message):
    if not condition:
        raise AutonomyContractError(message)


def _environment(root, declared):
    _require(type(declared) is StaticTeacherSimEnvironment, "typed static environment required")
    current = load_static_teacher_environment(
        artifact_root=root,
        generated_sdf_source=declared.generated_sdf_source,
        world_source=declared.scene.world_source,
        include_model_sources=dict(declared.scene.include_sources),
    )
    _require(
        canonical_json(current.to_mapping()) == canonical_json(declared.to_mapping()),
        "environment differs from complete source recomputation",
    )
    return current


def _sources(environment, models, extraction):
    values = [
        environment.scene.world_source,
        *dict(environment.scene.include_sources).values(),
        environment.generated_sdf_source,
        *models.values(),
        extraction,
    ]
    unique = {}
    for source in values:
        if source.path in unique:
            previous = unique[source.path]
            _require(
                (previous.bytes, previous.sha256) == (source.bytes, source.sha256),
                "conflicting shared source binding",
            )
        unique[source.path] = source
    return tuple(unique[key] for key in sorted(unique))


def _contains(support, body, radius):
    # Sphere projection encloses every orientation, with no nominal padding
    # simplification. Even a plane query stays inside its declared rectangle.
    delta = (body[0] - support.center_enu_m[0], body[1] - support.center_enu_m[1], 0)
    local = tuple(sum(support.rotation[j][i] * delta[j] for j in range(3)) for i in range(3))
    if support.kind == "cylinder":
        return math.hypot(*local[:2]) + radius <= support.half_extents_local_m[0]
    return all(abs(local[i]) + radius <= support.half_extents_local_m[i] for i in (0, 1))


@dataclass(frozen=True, slots=True)
class SignedGroundContactGeometryAudit:
    _document_json: str

    def to_mapping(self):
        return strict_json_loads(self._document_json)

    @property
    def canonical_hash(self):
        return canonical_sha256(self.to_mapping())

    @property
    def geometry_compatible(self):
        """A retained geometry fact, NOT ground-state or flight admission."""
        return self.to_mapping()["geometry_compatible"]


def audit_signed_ground_contact_geometry(
    *,
    artifact_root: str | Path,
    environment: StaticTeacherSimEnvironment,
    model_sources: dict,
    extraction_source,
    support_collision_id: str,
    maximum_ground_tilt_deg: float = 1.0,
) -> SignedGroundContactGeometryAudit:
    """Reopen full sources, measure signed gap, and retain explicit refusals.

    Bad sources or unsupported primitives/frames raise. Valid but geometrically
    incompatible sources produce a refused immutable audit with reasons. No
    caller-provided audit JSON, verification boolean or normalized pose is used.
    """
    maximum_tilt = require_real(
        maximum_ground_tilt_deg, "maximum_ground_tilt_deg", minimum=0, maximum=5
    )
    root = Path(artifact_root).resolve(strict=True)
    _require(
        type(support_collision_id) is str and 1 <= len(support_collision_id) <= 512,
        "explicit named support collision required",
    )
    _require(
        type(environment) is StaticTeacherSimEnvironment
        and type(model_sources) is dict
        and set(model_sources) == {"x500_mono_cam", "x500", "x500_base"}
        and all(type(v) is EvidenceBinding for v in model_sources.values())
        and type(extraction_source) is EvidenceBinding,
        "typed full environment and exact three-model source closure required",
    )
    sources = _sources(environment, model_sources, extraction_source)
    identities = {}
    for source in sources:
        _bound_bytes(root, source, MAXIMUM_SOURCE_BYTES, identities)
    current = _environment(root, environment)
    airframe = audit_x500_collision_envelope(
        artifact_root=root,
        model_sources=model_sources,
        extraction_source=extraction_source,
        generated_sdf_source=current.generated_sdf_source,
        maximum_tilt_deg=180.0,
    )
    models, offset, collisions, _, points = _models(root, model_sources, identities)
    _, vehicle, _, _ = _loaded(
        root, current.generated_sdf_source, collisions, offset, models, identities
    )
    rotation, model_position = _pose(vehicle)
    body_delta = _mv(rotation, offset)
    body = tuple(model_position[i] + body_delta[i] for i in range(3))
    rigid_bottom = min(body[2] + _mv(rotation, point)[2] for point in points)
    rotor_bottoms = []
    for key, collision in collisions.items():
        if key.startswith("rotor_"):
            center = tuple(collision.center_enu_m[i] - offset[i] for i in range(3))
            hx, hy, hz = collision.half_extents_local_m
            # Full revolute circle, not the blade's instantaneous serialized yaw.
            swept_vertical_extent = (
                math.hypot(rotation[2][0], rotation[2][1]) * math.hypot(hx, hy)
                + abs(rotation[2][2]) * hz
            )
            rotor_bottoms.append(body[2] + _mv(rotation, center)[2] - swept_vertical_extent)
    _require(len(rotor_bottoms) == 4, "complete four-rotor sweep required")
    bottom = min(rigid_bottom, *rotor_bottoms)
    tilt = math.degrees(math.acos(max(-1.0, min(1.0, rotation[2][2]))))
    support = next(
        (c for c in current.scene.collisions if c.collision_id == support_collision_id), None
    )
    _require(support is not None, "named support absent from recomputed static scene")
    _require(
        support.kind in {"plane", "box", "cylinder"}
        and all(abs(support.rotation[i][2] - _IDENTITY[i][2]) <= 1e-9 for i in range(3)),
        "only reviewed level plane/box/cylinder support surfaces are supported",
    )
    radius = airframe.envelope_bounds["horizontal_radius_m"]
    contains = _contains(support, body, radius)
    gap = bottom - support.upper_surface_up_m
    reasons = []
    if tilt > maximum_tilt:
        reasons.append("saved_body_tilt_outside_explicit_ground_domain")
    if not contains:
        reasons.append("support_does_not_contain_all_orientation_footprint")
    if gap < -NUMERICAL_PENETRATION_ALLOWANCE_M:
        reasons.append("penetration_exceeds_predeclared_numerical_allowance")
    if gap > POSITIVE_GAP_ALLOWANCE_M:
        reasons.append("positive_gap_exceeds_predeclared_contact_geometry_allowance")
    row = {
        "contract": CONTRACT,
        "schema_version": 1,
        "runtime_kind": "SIM",
        "geometry_compatible": not reasons,
        "refusal_reasons": reasons,
        "scope": "retained source geometry only, no freshness or ground-state admission",
        "environment_sha256": current.canonical_hash,
        "source_derived_airframe_audit_sha256": airframe.canonical_hash,
        "generated_sdf_source": current.generated_sdf_source.to_mapping(),
        "source_bindings": [s.to_mapping() for s in sources],
        "support_collision": support.to_mapping(),
        "saved_model_position_enu_m": list(model_position),
        "source_derived_body_reference_enu_m": list(body),
        "source_derived_rigid_collision_bottom_up_m": rigid_bottom,
        "source_derived_full_rotor_sweep_bottom_up_m": min(rotor_bottoms),
        "source_derived_all_collision_bottom_up_m": bottom,
        "support_surface_up_m": support.upper_surface_up_m,
        "signed_bottom_minus_support_up_m": gap,
        "predeclared_interval_m": [-NUMERICAL_PENETRATION_ALLOWANCE_M, POSITIVE_GAP_ALLOWANCE_M],
        "allowance_basis": "explicit numerical geometry policy, NOT inferred from ODE min_depth",
        "saved_body_up_tilt_deg": tilt,
        "maximum_ground_tilt_deg": maximum_tilt,
        "future_ground_tilt_enforcement_verified": False,
        "all_orientation_footprint_radius_m": radius,
        "footprint_is_nominal_geometry_not_navigation_uncertainty_tube": True,
        "support_contains_conservative_footprint": contains,
        "contact_force_or_solver_measurements_required_by_geometry_contract": False,
        "future_live_requirements": [
            "fresh source-qualified reference and observed original pose",
            "disarmed/onGround state from qualified sender",
            "bounded settled height/velocity window with unchanged estimator resets",
            "named support and reference uncertainty within checked footprint",
            "independent scoped takeoff/landing and full airborne route clearance",
        ],
        "claims": {
            key: False
            for key in (
                "live_freshness_verified",
                "source_authenticated",
                "ground_state_qualified",
                "contact_force_observed",
                "selected_solver_backend_verified",
                "solver_contact_PASS",
                "navigation_origin_qualified",
                "takeoff_or_landing_clearance_PASS",
                "whole_route_clearance_PASS",
                "OperatorGrant_issued",
                "Mission_approved",
                "ARM_sent",
                "flight_executed",
                "physical_airframe_calibrated",
                "final_release_allowed",
            )
        },
    }
    repeated_environment = _environment(root, environment)
    repeated_airframe = audit_x500_collision_envelope(
        artifact_root=root,
        model_sources=model_sources,
        extraction_source=extraction_source,
        generated_sdf_source=current.generated_sdf_source,
        maximum_tilt_deg=180.0,
    )
    _require(
        repeated_environment.canonical_hash == current.canonical_hash
        and repeated_airframe.canonical_hash == airframe.canonical_hash,
        "source geometry changed before contact audit admission",
    )
    for source in sources:
        _bound_bytes(root, source, MAXIMUM_SOURCE_BYTES, identities)
    return SignedGroundContactGeometryAudit(canonical_json(row))


def validate_signed_ground_contact_geometry(audit, **inputs):
    if type(audit) is not SignedGroundContactGeometryAudit:
        raise TypeError("exact SignedGroundContactGeometryAudit required")
    current = audit_signed_ground_contact_geometry(**inputs)
    if canonical_json(current.to_mapping()) != canonical_json(audit.to_mapping()):
        raise AutonomyContractError("stored contact geometry differs from source recomputation")


__all__ = [
    "SignedGroundContactGeometryAudit",
    "audit_signed_ground_contact_geometry",
    "validate_signed_ground_contact_geometry",
]
