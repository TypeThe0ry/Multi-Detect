"""Source-bound static teacher route authoring, not a flight Grant.

Near-ground support departure/approach is a separately declared conditional
design domain. Its feet are source-recomputed rigid/rotor geometry, never the
all-SO3 airborne sphere. Runtime enforcement of that domain is NOT established
here, so successful shared Mission authoring still has execution_ready=False.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from ..px4_mission import MultirotorMissionGeometry, Px4MultirotorSafetyPolicy
from .contracts import PlanAction, ResolvedTaskSpec, TaskType
from .mission_context_admission import ContextV2MissionAdmission
from .onboard_executed_trajectory import EvidenceBinding
from .onboard_teacher_mission import HeldTeacherLeg, compile_teacher_held_mission
from .planner import PlannerPolicy
from .serialization import (
    AutonomyContractError,
    canonical_json,
    canonical_sha256,
    require_real,
    strict_json_loads,
)
from .sim_airframe_envelope import _models, audit_x500_collision_envelope
from .sim_ground_contact_geometry import (
    NUMERICAL_PENETRATION_ALLOWANCE_M,
    POSITIVE_GAP_ALLOWANCE_M,
    _contains,
    _environment,
    audit_signed_ground_contact_geometry,
)
from .sim_navigation_reference import _camera, _qrotate
from .sim_planning_inputs import qualify_sim_planning_context
from .sim_static_route_clearance import (
    _bound_bytes,
    _local_geodetic_offset_enu,
    _segment_hits_box,
)
from .sim_teacher_static_admission import audit_static_teacher_resources
from .teacher_live_navigation_checkpoint import (
    LiveTeacherNavigationCheckpoint,
    validate_live_teacher_navigation_checkpoint,
)

CONTRACT = "multidetect.source-bound-static-teacher-route-admission-v1"


def _check(condition, message):
    if not condition:
        raise AutonomyContractError(message)


def _current_writer_time(checkpoint, requested_at_s=None):
    """Historical equality replay must not authorize current authoring."""
    capture, clock = checkpoint._live_capture, checkpoint._live_clock
    _check(
        capture is not None and clock is not None,
        "current authoring requires actual live writer/clock handles",
    )
    capture._open()
    _check(
        capture._clock == clock.monotonic
        and capture.clock_id == clock.session_id == checkpoint.clock_id,
        "actual live writer/clock identity differs",
    )
    current = require_real(clock.monotonic(), "actual authoring clock", minimum=0)
    _check(
        checkpoint.qualified_at_s <= current < checkpoint.expires_at_s,
        "actual authoring inputs expired or clock regressed",
    )
    if requested_at_s is not None:
        requested = require_real(requested_at_s, "requested authoring time", minimum=0)
        _check(
            0 <= current - requested <= 0.05,
            "caller authoring time is not current on original clock",
        )
    return current


@dataclass(frozen=True, slots=True)
class StaticTeacherRoutePolicy:
    horizontal_tracking_margin_m: float = 0.1
    vertical_tracking_margin_m: float = 0.1
    gps_accuracy_multiplier: float = 1.0
    projection_margin_m: float = 0.1
    ground_tilt_domain_deg: float = 1.0
    contact_transition_height_m: float = 1.0
    maximum_settled_position_span_m: float = 0.02
    maximum_ground_speed_mps: float = 0.1
    support_collision_id: str = "multirotor_home_pad/pad_link/pad_collision"

    def __post_init__(self):
        for name, minimum, maximum in (
            ("horizontal_tracking_margin_m", 0.05, 5),
            ("vertical_tracking_margin_m", 0.05, 5),
            ("gps_accuracy_multiplier", 1, 10),
            ("projection_margin_m", 0.1, 2),
            ("ground_tilt_domain_deg", 0, 1),
            ("contact_transition_height_m", 0.5, 3),
            ("maximum_settled_position_span_m", 0.001, 0.02),
            ("maximum_ground_speed_mps", 0.001, 0.1),
        ):
            object.__setattr__(
                self,
                name,
                require_real(getattr(self, name), name, minimum=minimum, maximum=maximum),
            )
        _check(
            type(self.support_collision_id) is str and 1 <= len(self.support_collision_id) <= 512,
            "named support required",
        )

    def to_mapping(self):
        return asdict(self)


@dataclass(frozen=True, slots=True)
class StaticTeacherRouteAdmission:
    _document_json: str

    def to_mapping(self):
        return strict_json_loads(self._document_json)

    @property
    def canonical_hash(self):
        return canonical_sha256(self.to_mapping())

    @property
    def static_design_clear(self):
        return self.to_mapping()["static_design_clear"]


def _fresh_ground(root, checkpoint, ground, models, identities, policy):
    inputs = checkpoint.to_mapping()["inputs"]
    updates = inputs["camera_updates"]
    _check(
        type(updates) is list and len(updates) == 2,
        "two current camera poses required to prove settled ground window",
    )
    _, offset, collisions, _, points = _models(root, models, identities)
    rows = []
    for update in updates:
        frame, source = _camera(root, update, checkpoint.clock_id, identities)
        q = frame["q_xyzw"]
        _check(
            math.sqrt(sum(v * v for v in frame["velocity_flu_mps"]))
            <= policy.maximum_ground_speed_mps,
            "actual camera velocity is outside settled ground scope",
        )
        delta = _qrotate(q, offset)
        body = tuple(frame["model_enu_m"][i] + delta[i] for i in range(3))
        tilt = math.degrees(math.acos(max(-1, min(1, _qrotate(q, (0, 0, 1))[2]))))
        bottom = min(body[2] + _qrotate(q, p)[2] for p in points)
        world_up = tuple(_qrotate(q, axis)[2] for axis in ((1, 0, 0), (0, 1, 0), (0, 0, 1)))
        rotor_bottoms = []
        for key, collision in collisions.items():
            if key.startswith("rotor_"):
                center = tuple(collision.center_enu_m[i] - offset[i] for i in range(3))
                hx, hy, hz = collision.half_extents_local_m
                extent = math.hypot(*world_up[:2]) * math.hypot(hx, hy) + abs(world_up[2]) * hz
                rotor_bottoms.append(body[2] + _qrotate(q, center)[2] - extent)
        _check(len(rotor_bottoms) == 4, "complete source-derived four rotor ground sweep required")
        bottom = min(bottom, *rotor_bottoms)
        gap = bottom - ground["support_surface_up_m"]
        _check(
            -NUMERICAL_PENETRATION_ALLOWANCE_M <= gap <= POSITIVE_GAP_ALLOWANCE_M
            and tilt <= policy.ground_tilt_domain_deg,
            "current original camera pose violates signed ground geometry/tilt domain",
        )
        _check(
            math.dist(body, ground["source_derived_body_reference_enu_m"])
            <= policy.maximum_settled_position_span_m,
            "current camera and source-recomputed saved ground reference differ",
        )
        rows.append(
            {
                "camera_source": source.to_mapping(),
                "received_at_s": frame["host_s"],
                "body_enu_m": list(body),
                "signed_gap_m": gap,
                "tilt_deg": tilt,
            }
        )
    _check(
        rows[0]["received_at_s"] < rows[1]["received_at_s"]
        and math.dist(rows[0]["body_enu_m"], rows[1]["body_enu_m"])
        <= policy.maximum_settled_position_span_m
        and checkpoint.observed_context.ground_speed_mps <= policy.maximum_ground_speed_mps,
        "ground pose/velocity is not a settled bounded window",
    )
    # Checkpoint's independently CRC-decoded ground state, LOCAL velocity and
    # listener reset/epoch continuity are revalidated by its shared producer.
    return rows


def audit_source_bound_static_teacher_route(
    *,
    checkpoint: LiveTeacherNavigationCheckpoint,
    artifact_root,
    environment,
    map_source: EvidenceBinding,
    dem_source: EvidenceBinding,
    extraction_source: EvidenceBinding,
    geometry: MultirotorMissionGeometry,
    safety_policy: Px4MultirotorSafetyPolicy,
    legs: tuple[HeldTeacherLeg, ...],
    now_s: float,
    clock_id: str,
    route_policy: StaticTeacherRoutePolicy | None = None,
) -> StaticTeacherRouteAdmission:
    """Recompute current inputs and complete static design clearance.

    The returned fact is conditional at ground contact. No stored audit flag,
    caller envelope scalar or whole-world ground exemption is accepted.
    """
    _check(type(checkpoint) is LiveTeacherNavigationCheckpoint, "typed live checkpoint required")
    _check(
        type(geometry) is MultirotorMissionGeometry
        and type(safety_policy) is Px4MultirotorSafetyPolicy,
        "typed geometry/policy required",
    )
    _check(
        type(legs) is tuple
        and 2 <= len(legs) <= 16
        and all(type(x) is HeldTeacherLeg for x in legs),
        "bounded held teacher legs required",
    )
    _check(
        len(legs) == len(geometry.waypoints)
        and tuple(x.sequence for x in legs) == tuple(range(1, len(legs) + 1)),
        "legs/waypoints differ",
    )
    policy = route_policy or StaticTeacherRoutePolicy()
    _check(type(policy) is StaticTeacherRoutePolicy, "typed route policy required")
    now = require_real(now_s, "now_s", minimum=0)
    root = Path(artifact_root).resolve(strict=True)
    validate_live_teacher_navigation_checkpoint(
        checkpoint, artifact_root=root, now_s=now, clock_id=clock_id
    )
    context, attestation, origin = (
        checkpoint.observed_context,
        checkpoint.runtime_attestation,
        checkpoint.origin,
    )
    _check(
        context.provenance.runtime_kind == "SIM"
        and context.camera_available
        and not context.perception_available,
        "bounded truth-free camera-only SIM checkpoint required",
    )
    current = _environment(root, environment)
    _check(
        current.scene.world_source == checkpoint.world_source,
        "checkpoint and static world sources differ",
    )
    model_sources = checkpoint.model_sources
    ground_audit = audit_signed_ground_contact_geometry(
        artifact_root=root,
        environment=current,
        model_sources=model_sources,
        extraction_source=extraction_source,
        support_collision_id=policy.support_collision_id,
        maximum_ground_tilt_deg=policy.ground_tilt_domain_deg,
    )
    _check(ground_audit.geometry_compatible, "source-recomputed signed ground geometry refused")
    ground = ground_audit.to_mapping()
    identities = {}
    fresh_ground = _fresh_ground(root, checkpoint, ground, model_sources, identities, policy)
    airborne = audit_x500_collision_envelope(
        artifact_root=root,
        model_sources=model_sources,
        extraction_source=extraction_source,
        generated_sdf_source=current.generated_sdf_source,
        maximum_tilt_deg=180,
    )
    near_ground = audit_x500_collision_envelope(
        artifact_root=root,
        model_sources=model_sources,
        extraction_source=extraction_source,
        generated_sdf_source=current.generated_sdf_source,
        maximum_tilt_deg=policy.ground_tilt_domain_deg,
    )
    bounds = airborne.envelope_bounds
    _check(
        airborne.to_mapping()["image"] == attestation.image,
        "runtime image differs from source-derived airframe image",
    )
    od = origin.to_mapping()
    home = context.home_position
    _check(
        (geometry.home.latitude_deg, geometry.home.longitude_deg)
        == (home.latitude_deg, home.longitude_deg),
        "geometry home differs from actual observed home",
    )
    final = geometry.waypoints[-1]
    _check(
        (final.latitude_deg, final.longitude_deg) == (home.latitude_deg, home.longitude_deg)
        and final.altitude_m > 0,
        "elevated home-return waypoint required",
    )
    _check(
        geometry.landing == replace(home, altitude_m=0.0, altitude_reference="relative_home"),
        "explicit vertical home LAND required",
    )
    reference, verified_home = od["world_reference"], origin.home_enu_m
    offset = _local_geodetic_offset_enu(context.aircraft_position, home, reference)
    gps = context.gps_horizontal_accuracy_m * policy.gps_accuracy_multiplier
    _check(
        math.hypot(*offset) <= gps + od["horizontal_uncertainty_m"],
        "actual aircraft/home offset exceeds uncertainty",
    )
    tube = (
        bounds["horizontal_radius_m"]
        + gps
        + od["horizontal_uncertainty_m"]
        + policy.horizontal_tracking_margin_m
        + policy.projection_margin_m
        + max(geometry.acceptance_radius_m, geometry.loiter_radius_m)
    )
    vertical = (
        policy.vertical_tracking_margin_m
        + od["vertical_uncertainty_m"]
        + geometry.acceptance_radius_m
    )
    resource_audit = audit_static_teacher_resources(
        artifact_root=root,
        environment=current,
        map_source=map_source,
        dem_source=dem_source,
        scenario_id=attestation.scenario_id,
        geometry=geometry,
        safety_policy=safety_policy,
        horizontal_tube_m=tube,
        vertical_tracking_uncertainty_m=vertical,
    )
    vertical = resource_audit.to_mapping()["vertical_clearance_margin_m"]
    support = next(
        c for c in current.scene.collisions if c.collision_id == policy.support_collision_id
    )
    start = (
        verified_home[0] + offset[0],
        verified_home[1] + offset[1],
        verified_home[2] + context.aircraft_position.altitude_m,
    )
    fence_offset = _local_geodetic_offset_enu(safety_policy.geofence_center, home, reference)
    fence_center = (verified_home[0] + fence_offset[0], verified_home[1] + fence_offset[1])
    _check(
        math.dist(start[:2], fence_center) + tube <= safety_policy.geofence_radius_m,
        "actual ground start tube leaves safety fence",
    )
    for center in (start, verified_home, *(r["body_enu_m"] for r in fresh_ground)):
        _check(
            _contains(support, center, tube),
            "full ground uncertainty footprint leaves named support",
        )
    transition = max(
        policy.contact_transition_height_m,
        support.upper_surface_up_m
        - verified_home[2]
        - bounds["bottom_offset_m"]
        + vertical
        + 0.001,
    )
    _check(
        geometry.takeoff_altitude_m > transition and final.altitude_m > transition,
        "takeoff/return altitude cannot clear all-attitude contact transition",
    )
    low_start = (start[0], start[1], verified_home[2] + transition)
    low_home = (verified_home[0], verified_home[1], verified_home[2] + transition)
    takeoff = (verified_home[0], verified_home[1], verified_home[2] + geometry.takeoff_altitude_m)
    points = []
    for point in geometry.waypoints:
        east, north = _local_geodetic_offset_enu(point, home, reference)
        points.append(
            (verified_home[0] + east, verified_home[1] + north, verified_home[2] + point.altitude_m)
        )
    intervals = [
        ("contact_departure", start, low_start, True),
        ("takeoff", low_start, takeoff, False),
    ]
    prior = takeoff
    for i, point in enumerate(points, 1):
        intervals.extend(
            ((f"transit-{i}", prior, point, False), (f"hold-{i}", point, point, False))
        )
        prior = point
    intervals.extend(
        (
            ("terminal_loiter", points[-1], points[-1], False),
            ("landing_approach", points[-1], low_home, False),
            ("contact_landing", low_home, tuple(verified_home), True),
        )
    )
    hits, exceptions, checked = [], [], []
    for phase, first, last, contact in intervals:
        b = near_ground.envelope_bounds if contact else bounds
        checked.append(
            {
                "phase": phase,
                "start_enu_m": list(first),
                "end_enu_m": list(last),
                "attitude_domain": "conditional_near_ground" if contact else "all_SO3",
            }
        )
        for collision in current.scene.collisions:
            below = (
                contact
                and collision.upper_surface_up_m <= support.upper_surface_up_m + 1e-9
                and collision.kind in {"plane", "box", "cylinder"}
                and all(abs(collision.rotation[i][2] - (0, 0, 1)[i]) <= 1e-9 for i in range(3))
                and all(_contains(collision, center, tube) for center in (first, last))
            )
            if below:
                exceptions.append(
                    {
                        "phase": phase,
                        "collision_id": collision.collision_id,
                        "basis": (
                            "bounded named-support approach/departure substrate; "
                            "conditional control domain"
                        ),
                    }
                )
                continue
            if collision.kind == "plane":
                # Finite plane coverage was separately proved by the strict
                # resource audit; it has no 3D AABB in the shared SDF parser.
                if (
                    min(first[2], last[2]) + b["bottom_offset_m"] - vertical
                    <= collision.upper_surface_up_m
                ):
                    hits.append({"phase": phase, "collision_id": collision.collision_id})
                continue
            minimum = (
                collision.minimum_enu_m[0] - tube,
                collision.minimum_enu_m[1] - tube,
                collision.minimum_enu_m[2] - b["top_offset_m"] - vertical,
            )
            maximum = (
                collision.maximum_enu_m[0] + tube,
                collision.maximum_enu_m[1] + tube,
                collision.maximum_enu_m[2] - b["bottom_offset_m"] + vertical,
            )
            if _segment_hits_box(first, last, minimum, maximum):
                hits.append({"phase": phase, "collision_id": collision.collision_id})
    row = {
        "contract": CONTRACT,
        "runtime_kind": "SIM",
        "vehicle_class": "multirotor",
        "checkpoint_sha256": checkpoint.canonical_hash,
        "checkpoint_source": checkpoint.checkpoint_source.to_mapping(),
        "observed_context_sha256": context.canonical_hash,
        "runtime_attestation_sha256": attestation.canonical_hash,
        "geometry_sha256": canonical_sha256(geometry.to_mapping()),
        "legs_sha256": canonical_sha256([x.to_mapping() for x in legs]),
        "safety_policy_sha256": canonical_sha256(safety_policy.to_mapping()),
        "route_policy": policy.to_mapping(),
        "qualified_at_s": now,
        "expires_at_s": checkpoint.expires_at_s,
        "clock_id": clock_id,
        "environment_sha256": current.canonical_hash,
        "ground_geometry": ground,
        "fresh_ground_poses": fresh_ground,
        "origin_sha256": origin.canonical_hash,
        "airborne_envelope": airborne.to_mapping(),
        "near_ground_envelope": near_ground.to_mapping(),
        "resource_audit": resource_audit.to_mapping(),
        "horizontal_tube_m": tube,
        "vertical_margin_m": vertical,
        "contact_transition_relative_home_m": transition,
        "audited_intervals": checked,
        "collision_hits": hits,
        "contact_phase_exceptions": exceptions,
        "static_design_clear": not hits,
        "execution_ready": False,
        "full_unconditional_route_clearance_qualified": False,
        "near_ground_support_contact_condition": {
            "signed_gap_interval_m": [-NUMERICAL_PENETRATION_ALLOWANCE_M, POSITIVE_GAP_ALLOWANCE_M],
            "pose_dependent_collision_bottom_required": True,
            "tilt_bound_alone_is_sufficient": False,
            "future_contact_pose_or_control_verified": False,
        },
        "unresolved_execution_requirements": [
            (
                "runtime-verified near-ground attitude/control "
                "and pose-dependent support-contact bounds"
            ),
            "normal ValidationReport, OperatorGrant and persistent Mission transaction admission",
        ],
        "claims": {
            name: False
            for name in (
                "perception_qualified",
                "dynamic_obstacles_covered",
                "contact_force_verified",
                "solver_verified",
                "actual_tilt_limit_enforced",
                "flight_authority",
                "mission_approved",
                "ARM_sent",
                "flight_executed",
                "final_release_allowed",
            )
        },
    }
    _environment(root, current)
    for source in (
        map_source,
        dem_source,
        extraction_source,
        *model_sources.values(),
        current.generated_sdf_source,
    ):
        _bound_bytes(root, source, 1024 * 1024, identities)
    validate_live_teacher_navigation_checkpoint(
        checkpoint, artifact_root=root, now_s=now, clock_id=clock_id
    )
    return StaticTeacherRouteAdmission(canonical_json(row))


def compile_source_bound_static_teacher_mission(
    *,
    checkpoint,
    task: ResolvedTaskSpec,
    model_source: EvidenceBinding,
    artifact_root,
    environment,
    map_source,
    dem_source,
    extraction_source,
    geometry,
    safety_policy,
    legs,
    now_s,
    clock_id,
    route_policy=None,
    planner_policy: PlannerPolicy | None = None,
    mission_id=None,
):
    """Return shared teacher draft plus exact current static/budget receipt.

    Raw context is retained separately. Only three resource booleans change
    after actual strict assets and complete route checks; observed sensor data,
    camera/perception flags and original epochs are never repaired/renewed.
    """
    _check(
        type(task) is ResolvedTaskSpec
        and task.task_type is TaskType.SURVEY
        and not task.on_detection
        and task.completion_action is PlanAction.RETURN
        and task.constraints.remain_inside_geofence,
        "bounded non-perception teacher collection task required",
    )
    _check(tuple(task.geometry.centerline) == geometry.waypoints, "teacher task and route differ")
    _check(type(checkpoint) is LiveTeacherNavigationCheckpoint, "typed live checkpoint required")
    _current_writer_time(checkpoint, now_s)
    identities = {}
    root = Path(artifact_root).resolve(strict=True)
    sources = (
        map_source,
        dem_source,
        extraction_source,
        model_source,
        checkpoint.checkpoint_source,
        environment.generated_sdf_source,
        environment.scene.world_source,
        *dict(environment.scene.include_sources).values(),
        *checkpoint.model_sources.values(),
    )
    for source in sources:
        _bound_bytes(root, source, 2 * 1024 * 1024, identities)
    options = dict(
        checkpoint=checkpoint,
        artifact_root=artifact_root,
        environment=environment,
        map_source=map_source,
        dem_source=dem_source,
        extraction_source=extraction_source,
        geometry=geometry,
        safety_policy=safety_policy,
        legs=legs,
        now_s=now_s,
        clock_id=clock_id,
        route_policy=route_policy,
    )
    route = audit_source_bound_static_teacher_route(**options)
    _check(route.static_design_clear, "full static teacher design route intersects an obstacle")
    compilation_at_s = _current_writer_time(checkpoint)
    original = checkpoint.observed_context
    context, energy = qualify_sim_planning_context(
        original,
        runtime_attestation=checkpoint.runtime_attestation,
        observation_source=checkpoint.observation_source,
        model_source=model_source,
        artifact_root=artifact_root,
        now_s=compilation_at_s,
        attested_at_s=checkpoint.inspected_at_s,
        clock_id=clock_id,
    )
    context = replace(context, map_cache_available=True, dem_available=True, geofence_valid=True)
    document = route.to_mapping()
    document.update(
        {
            "static_audited_at_s": now_s,
            "qualified_at_s": compilation_at_s,
            "original_observed_context": original.to_mapping(),
            "energy_qualification": energy.to_mapping(),
            "derived_planning_context": context.to_mapping(),
            "derived_planning_context_sha256": context.canonical_hash,
            "task_sha256": task.canonical_hash,
        }
    )
    qualification = StaticTeacherRouteAdmission(canonical_json(document))
    admission = ContextV2MissionAdmission(
        context_snapshot_sha256=context.canonical_hash,
        task_spec_sha256=task.canonical_hash,
        geometry_sha256=canonical_sha256(geometry.to_mapping()),
        safety_policy_sha256=canonical_sha256(safety_policy.to_mapping()),
        airframe_profile_id=context.airframe_profile_id,
        airframe_profile_sha256=context.airframe_profile_hash,
        runtime_attestation_sha256=checkpoint.runtime_attestation.canonical_hash,
        planning_qualification_sha256=qualification.canonical_hash,
        runtime_kind="SIM",
        vehicle_class="multirotor",
        clock_id=clock_id,
        observed_at_s=original.captured_at_s,
        qualified_at_s=compilation_at_s,
        expires_at_s=min(
            checkpoint.expires_at_s,
            original.captured_at_s + energy.maximum_age_s,
            energy.attested_at_s + energy.maximum_age_s,
        ),
    )
    draft = compile_teacher_held_mission(
        task=task,
        context=context,
        geometry=geometry,
        safety_policy=safety_policy,
        legs=legs,
        airframe_profile_hash=context.airframe_profile_hash,
        planner_policy=planner_policy,
        mission_id=mission_id,
        context_admission=admission,
        admission_now_s=compilation_at_s,
        admission_clock_id=clock_id,
    )
    # Returning an artifact must not consume a provisional changed source row.
    for source in sources:
        _bound_bytes(root, source, 2 * 1024 * 1024, identities)
    _environment(root, environment)
    final_now = _current_writer_time(checkpoint)
    validate_live_teacher_navigation_checkpoint(
        checkpoint, artifact_root=root, now_s=final_now, clock_id=clock_id
    )
    _current_writer_time(checkpoint)
    return draft, qualification


def validate_source_bound_static_teacher_route(admission, **inputs):
    _check(type(admission) is StaticTeacherRouteAdmission, "typed route admission required")
    current = audit_source_bound_static_teacher_route(**inputs)
    _check(
        canonical_json(current.to_mapping()) == canonical_json(admission.to_mapping()),
        "static route admission recomputation differs",
    )


def validate_source_bound_static_teacher_mission(draft, admission, **inputs):
    """Full source/budget reauthoring, never a live issuer or authority token."""
    _check(
        type(admission) is StaticTeacherRouteAdmission, "typed static Mission admission required"
    )
    current_draft, current = compile_source_bound_static_teacher_mission(**inputs)
    _check(
        canonical_json(current.to_mapping()) == canonical_json(admission.to_mapping())
        and canonical_json(current_draft.to_mapping()) == canonical_json(draft.to_mapping()),
        "static Mission/source admission recomputation differs",
    )


__all__ = [
    "StaticTeacherRoutePolicy",
    "StaticTeacherRouteAdmission",
    "audit_source_bound_static_teacher_route",
    "compile_source_bound_static_teacher_mission",
    "validate_source_bound_static_teacher_route",
    "validate_source_bound_static_teacher_mission",
]
