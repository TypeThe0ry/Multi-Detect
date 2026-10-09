"""Strict source-derived resources for bounded static multirotor SIM authoring.

The cached GeoJSON is an authored operating-area boundary, not a perception
map. The DEM is an MSL base-ground grid, not a building/contact model. These
facts qualify only the explicitly checked route and circular safety policy;
they neither upload a PX4 fence nor grant flight authority.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from ..px4_mission import MultirotorMissionGeometry, Px4MultirotorSafetyPolicy
from ..terrain_dem import load_local_dem_grid
from .onboard_executed_trajectory import EvidenceBinding
from .serialization import (
    AutonomyContractError,
    canonical_json,
    canonical_sha256,
    require_real,
    require_schema_version,
    require_text,
    strict_json_loads,
    strict_object,
)
from .sim_ground_contact_geometry import _contains, _environment
from .sim_static_route_clearance import _bound_bytes
from .sim_teacher_environment import StaticTeacherSimEnvironment

CONTRACT = "multidetect.source-derived-static-teacher-SIM-resources-v1"
MAXIMUM_RESOURCE_BYTES = 1024 * 1024


def _check(condition, message):
    if not condition:
        raise AutonomyContractError(message)


def _rectangle(raw, scenario):
    value = strict_object(
        raw,
        name="teacher operating-area GeoJSON",
        required=frozenset(
            {"type", "schema_version", "scenario_id", "coordinate_reference", "features"}
        ),
    )
    require_schema_version(value["schema_version"])
    _check(
        value["type"] == "FeatureCollection"
        and value["scenario_id"] == scenario
        and value["coordinate_reference"] == "WGS84",
        "map scenario/frame identity differs",
    )
    features = value["features"]
    _check(
        type(features) is list and len(features) == 1, "exactly one operating-area feature required"
    )
    feature = strict_object(
        features[0],
        name="operating-area feature",
        required=frozenset({"type", "id", "properties", "geometry"}),
    )
    _check(feature["type"] == "Feature", "GeoJSON Feature required")
    require_text(feature["id"], "operating-area feature id")
    properties = strict_object(
        feature["properties"],
        name="operating-area properties",
        required=frozenset({"role", "source"}),
    )
    _check(
        properties["role"] == "operating_area"
        and properties["source"] == "repository_owned_simulation_asset",
        "map does not declare the supported authored operating area",
    )
    geometry = strict_object(
        feature["geometry"],
        name="operating-area geometry",
        required=frozenset({"type", "coordinates"}),
    )
    rings = geometry["coordinates"]
    _check(
        geometry["type"] == "Polygon"
        and type(rings) is list
        and len(rings) == 1
        and type(rings[0]) is list
        and len(rings[0]) == 5,
        "only a bounded rectangular Polygon without holes is supported",
    )
    points = []
    for point in rings[0]:
        _check(type(point) is list and len(point) == 2, "exact lon/lat pair required")
        points.append(
            (
                require_real(point[0], "map longitude", minimum=-180, maximum=180),
                require_real(point[1], "map latitude", minimum=-89, maximum=89),
            )
        )
    _check(points[0] == points[-1] and len(set(points[:4])) == 4, "closed unique map ring required")
    west, east = min(p[0] for p in points), max(p[0] for p in points)
    south, north = min(p[1] for p in points), max(p[1] for p in points)
    _check(
        0 < east - west < 0.02
        and 0 < north - south < 0.02
        and set(points[:4]) == {(west, south), (west, north), (east, south), (east, north)}
        and all(a[0] == b[0] or a[1] == b[1] for a, b in zip(points, points[1:], strict=False)),
        "map polygon is not the supported small axis-aligned rectangle",
    )
    return west, south, east, north


@dataclass(frozen=True, slots=True)
class StaticTeacherResourceAudit:
    _document_json: str

    def to_mapping(self):
        return strict_json_loads(self._document_json)

    @property
    def canonical_hash(self):
        return canonical_sha256(self.to_mapping())


def audit_static_teacher_resources(
    *,
    artifact_root,
    environment: StaticTeacherSimEnvironment,
    map_source: EvidenceBinding,
    dem_source: EvidenceBinding,
    scenario_id: str,
    geometry: MultirotorMissionGeometry,
    safety_policy: Px4MultirotorSafetyPolicy,
    horizontal_tube_m: float,
    vertical_tracking_uncertainty_m: float,
    dem_sigma_multiplier: float = 3.0,
) -> StaticTeacherResourceAudit:
    """Reopen strict assets and prove complete convex-route/fence coverage.

    This deliberately narrow first contract supports the actual flat urban
    DEM. Non-flat terrain requires a separate source-derived terrain sweep;
    it is not silently approximated by endpoint samples.
    """
    root = Path(artifact_root).resolve(strict=True)
    _check(type(environment) is StaticTeacherSimEnvironment, "typed static environment required")
    _check(type(geometry) is MultirotorMissionGeometry, "typed MR geometry required")
    _check(type(safety_policy) is Px4MultirotorSafetyPolicy, "typed MR safety policy required")
    scenario = require_text(scenario_id, "scenario_id")
    radius = require_real(horizontal_tube_m, "horizontal_tube_m", minimum=0.1, maximum=20)
    vertical = require_real(
        vertical_tracking_uncertainty_m,
        "vertical_tracking_uncertainty_m",
        minimum=0,
        maximum=10,
    )
    sigma = require_real(dem_sigma_multiplier, "dem_sigma_multiplier", minimum=3, maximum=6)
    identities = {}
    for source in (map_source, dem_source):
        _check(
            type(source) is EvidenceBinding and source.json_pointer == "",
            "whole resource JSON required",
        )
        _bound_bytes(root, source, MAXIMUM_RESOURCE_BYTES, identities)
    current = _environment(root, environment)
    rectangle = _rectangle(
        strict_json_loads(_bound_bytes(root, map_source, MAXIMUM_RESOURCE_BYTES, identities)),
        scenario,
    )
    grid = load_local_dem_grid(root / dem_source.path)
    _check(
        grid.source_sha256 == dem_source.sha256 and grid.scenario_id == scenario,
        "DEM identity differs",
    )
    scene = current.scene
    _check(
        (grid.origin_latitude_deg, grid.origin_longitude_deg, grid.origin_elevation_m)
        == (
            scene.reference_latitude_deg,
            scene.reference_longitude_deg,
            scene.reference_elevation_m,
        ),
        "DEM WGS84/MSL reference differs from source world ENU reference",
    )
    _check(
        all(abs(z - grid.origin_elevation_m) <= 1e-6 for row in grid.elevations_m for z in row),
        "non-flat DEM needs a full terrain sweep; endpoint-only qualification is forbidden",
    )
    fence = safety_policy.geofence_center
    fence_n, fence_e = grid.local_from_wgs84(fence.latitude_deg, fence.longitude_deg)
    fence_radius = safety_policy.geofence_radius_m
    # Curvature/ECEF-vs-grid projection uncertainty is explicit and separate
    # from GPS/tracking margins. The local domain is bounded to one kilometre.
    projection_allowance = 0.1
    coverage_radius = fence_radius + radius + projection_allowance
    _check(coverage_radius <= 500, "bounded teacher resource domain exceeds 500 metres")
    west, south, east, north = rectangle
    map_corners = [
        grid.local_from_wgs84(lat, lon) for lon in (west, east) for lat in (south, north)
    ]
    map_min_n, map_max_n = min(p[0] for p in map_corners), max(p[0] for p in map_corners)
    map_min_e, map_max_e = min(p[1] for p in map_corners), max(p[1] for p in map_corners)
    # Inscribed rectangle, not the original weak origin-in-bounding-box gate.
    # East scale varies with latitude: use the intersection of both parallels.
    map_min_e = max(grid.local_from_wgs84(lat, west)[1] for lat in (south, north))
    map_max_e = min(grid.local_from_wgs84(lat, east)[1] for lat in (south, north))
    for minimum_n, maximum_n, minimum_e, maximum_e, label in (
        (map_min_n, map_max_n, map_min_e, map_max_e, "map"),
        (grid.north_origin_m, grid.north_max_m, grid.east_origin_m, grid.east_max_m, "DEM"),
    ):
        _check(
            minimum_n <= fence_n - coverage_radius <= fence_n + coverage_radius <= maximum_n
            and minimum_e <= fence_e - coverage_radius <= fence_e + coverage_radius <= maximum_e,
            f"full safety fence plus route uncertainty lies outside {label} coverage",
        )
    planes = [c for c in scene.collisions if c.kind == "plane"]
    covering = [
        c
        for c in planes
        if abs(c.upper_surface_up_m) <= 1e-6
        and max(abs(c.rotation[i][2] - (0, 0, 1)[i]) for i in range(3)) <= 1e-9
        and _contains(c, (fence_e, fence_n, 0), coverage_radius)
    ]
    _check(covering, "finite loaded base-ground plane does not cover full fence/uncertainty")
    coordinates = (geometry.home, *geometry.waypoints, geometry.landing)
    route = []
    for point in coordinates:
        n, e = grid.local_from_wgs84(point.latitude_deg, point.longitude_deg)
        _check(
            math.hypot(n - fence_n, e - fence_e) + radius <= fence_radius,
            "route tube leaves authored fence",
        )
        sample = grid.sample_local(n, e)
        route.append({"north_m": n, "east_m": e, "base_ground_MSL_m": sample.elevation_m})
    vertical_margin = vertical + sigma * grid.vertical_sigma_m + radius * grid.slope_sigma_m_per_m
    row = {
        "contract": CONTRACT,
        "runtime_kind": "SIM",
        "vehicle_class": "multirotor",
        "scenario_id": scenario,
        "environment_sha256": current.canonical_hash,
        "map_source": map_source.to_mapping(),
        "dem_source": dem_source.to_mapping(),
        "geometry_sha256": canonical_sha256(geometry.to_mapping()),
        "safety_policy_sha256": canonical_sha256(safety_policy.to_mapping()),
        "map_kind": "authored rectangular operating boundary, not perception",
        "DEM_kind": "flat MSL base-ground grid; loaded obstacles handled independently",
        "grid_frame": "local north/east metres from WGS84 origin",
        "world_frame": "ENU, zero heading; world elevation equals DEM MSL origin",
        "coverage_basis": "convex fence and all route segments plus full uncertainty tube",
        "horizontal_tube_m": radius,
        "projection_allowance_m": projection_allowance,
        "DEM_vertical_sigma_m": grid.vertical_sigma_m,
        "DEM_slope_sigma_m_per_m": grid.slope_sigma_m_per_m,
        "DEM_sigma_multiplier": sigma,
        "vertical_clearance_margin_m": vertical_margin,
        "finite_base_ground_collision_ids": [c.collision_id for c in covering],
        "route_samples": route,
        "qualified_resources": {
            "map_cache_available": True,
            "dem_available": True,
            "geofence_valid": True,
        },
        "geofence_kind": (
            "source-covered authored safety-policy fence, not autopilot upload/readback"
        ),
        "claims": {
            name: False
            for name in (
                "perception_available",
                "dynamic_obstacles_covered",
                "PX4_fence_uploaded",
                "contact_verified",
                "route_collision_clear",
                "execution_authority",
                "mission_approved",
                "hardware_control_enabled",
                "final_release_allowed",
            )
        },
    }
    _environment(root, current)
    for source in (map_source, dem_source):
        _bound_bytes(root, source, MAXIMUM_RESOURCE_BYTES, identities)
    return StaticTeacherResourceAudit(canonical_json(row))


def validate_static_teacher_resources(audit, **inputs):
    _check(type(audit) is StaticTeacherResourceAudit, "typed resource audit required")
    current = audit_static_teacher_resources(**inputs)
    _check(
        canonical_json(current.to_mapping()) == canonical_json(audit.to_mapping()),
        "resource audit recomputation differs",
    )


__all__ = [
    "StaticTeacherResourceAudit",
    "audit_static_teacher_resources",
    "validate_static_teacher_resources",
]
