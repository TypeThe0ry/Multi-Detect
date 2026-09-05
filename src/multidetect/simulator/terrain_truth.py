"""Independent Gazebo SDF plane truth for deterministic terrain-ranging QA.

This module is acceptance infrastructure, not a simulator-only estimator.  It
parses one repository-owned SDF plane independently from :mod:`terrain_dem`
and compares the shared bilinear DEM sampler/intersector with analytic plane
truth.  No Gazebo process, hardware, operator UI, or flight-control path is
opened by this code.
"""

from __future__ import annotations

import hashlib
import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..terrain_dem import DemRayIntersection, LocalDemGrid

_MAX_SDF_BYTES = 4 * 1024 * 1024
_NETWORK_OR_EXTERNAL_URI_RE = re.compile(
    r"(?:https?|ftp|fuel|model)://",
    re.IGNORECASE,
)
_FORBIDDEN_XML_DECLARATION_RE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_TRUTH_MODEL_NAME = "multidetect_dem_truth_surface"
_ROOT_NAME = "default"
_VECTOR_TOLERANCE = 1.0e-12
_ORIGIN_ANGLE_TOLERANCE_DEG = 1.0e-9
_ORIGIN_ELEVATION_TOLERANCE_M = 1.0e-9
_NON_FLAT_SLOPE_MINIMUM = 1.0e-6

DEFAULT_TOLERANCES: dict[str, float] = {
    "elevation_m": 1.0e-9,
    "slope_m_per_m": 1.0e-12,
    "ray_slant_range_m": 1.0e-7,
    "ray_north_m": 1.0e-7,
    "ray_east_m": 1.0e-7,
    "ray_elevation_m": 1.0e-7,
    "ray_incidence_component": 1.0e-7,
}


class SimulatorTerrainTruthError(ValueError):
    """Raised when SDF truth is malformed, ambiguous, or unsafe to compare."""


@dataclass(frozen=True, slots=True)
class TerrainTruthRayIntersection:
    slant_range_m: float
    north_m: float
    east_m: float
    elevation_m: float
    incidence_component: float

    def __post_init__(self) -> None:
        values = (
            self.slant_range_m,
            self.north_m,
            self.east_m,
            self.elevation_m,
            self.incidence_component,
        )
        if not all(math.isfinite(value) for value in values):
            raise SimulatorTerrainTruthError("truth ray intersection values must be finite")
        if self.slant_range_m <= 0.0 or self.incidence_component <= 0.0:
            raise SimulatorTerrainTruthError("truth ray intersection must be forward and downward")

    def to_mapping(self) -> dict[str, float]:
        return {
            "slant_range_m": self.slant_range_m,
            "north_m": self.north_m,
            "east_m": self.east_m,
            "elevation_m": self.elevation_m,
            "incidence_component": self.incidence_component,
        }


@dataclass(frozen=True, slots=True)
class TerrainTruthPlane:
    scenario_id: str
    sdf_version: str
    world_name: str
    surface_model: str
    world_frame_orientation: str
    origin_latitude_deg: float
    origin_longitude_deg: float
    origin_elevation_m: float
    model_name: str
    model_pose_enu_rpy_rad: tuple[float, float, float, float, float, float]
    model_normal_enu: tuple[float, float, float]
    plane_size_m: tuple[float, float]
    rotation_enu_from_model: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ]
    source_sha256: str
    source_bytes: int

    def __post_init__(self) -> None:
        if not self.scenario_id.strip() or not self.sdf_version.strip():
            raise SimulatorTerrainTruthError("truth scenario and SDF version cannot be empty")
        if self.world_name != _ROOT_NAME:
            raise SimulatorTerrainTruthError("truth SDF world must use the default namespace")
        if self.surface_model != "EARTH_WGS84":
            raise SimulatorTerrainTruthError("truth SDF must use EARTH_WGS84")
        if self.world_frame_orientation != "ENU":
            raise SimulatorTerrainTruthError("truth SDF must use the ENU world frame")
        if not -90.0 < self.origin_latitude_deg < 90.0:
            raise SimulatorTerrainTruthError("truth latitude is outside the WGS-84 domain")
        if not -180.0 <= self.origin_longitude_deg <= 180.0:
            raise SimulatorTerrainTruthError("truth longitude is outside the WGS-84 domain")
        if not -500.0 <= self.origin_elevation_m <= 9_000.0:
            raise SimulatorTerrainTruthError("truth elevation is outside supported bounds")
        if self.model_name != _TRUTH_MODEL_NAME:
            raise SimulatorTerrainTruthError("truth plane model identity is invalid")
        numeric = (
            *self.model_pose_enu_rpy_rad,
            *self.model_normal_enu,
            *self.plane_size_m,
            *(item for row in self.rotation_enu_from_model for item in row),
        )
        if not all(math.isfinite(value) for value in numeric):
            raise SimulatorTerrainTruthError("truth plane geometry must be finite")
        if len(self.model_pose_enu_rpy_rad) != 6:
            raise SimulatorTerrainTruthError("truth plane pose must contain six values")
        if len(self.model_normal_enu) != 3 or len(self.plane_size_m) != 2:
            raise SimulatorTerrainTruthError("truth plane vectors have invalid dimensions")
        if any(value <= 0.0 or value > 1_000_000.0 for value in self.plane_size_m):
            raise SimulatorTerrainTruthError("truth plane size is outside supported bounds")
        normal_norm = math.sqrt(sum(value * value for value in self.model_normal_enu))
        if not math.isclose(normal_norm, 1.0, rel_tol=0.0, abs_tol=1.0e-12):
            raise SimulatorTerrainTruthError("truth plane normal must be unit length")
        if self.model_normal_enu[2] <= 0.05:
            raise SimulatorTerrainTruthError(
                "truth plane is vertical, inverted, or numerically unsafe"
            )
        digest = self.source_sha256.lower()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise SimulatorTerrainTruthError("truth SDF SHA-256 is invalid")
        if self.source_bytes <= 0 or self.source_bytes > _MAX_SDF_BYTES:
            raise SimulatorTerrainTruthError("truth SDF byte count is invalid")
        object.__setattr__(self, "source_sha256", digest)

    @property
    def east_slope_m_per_m(self) -> float:
        normal_east, _normal_north, normal_up = self.model_normal_enu
        return -normal_east / normal_up

    @property
    def north_slope_m_per_m(self) -> float:
        _normal_east, normal_north, normal_up = self.model_normal_enu
        return -normal_north / normal_up

    @property
    def surface_elevation_at_local_zero_msl(self) -> float:
        return self.elevation_msl(0.0, 0.0)

    @property
    def is_non_flat(self) -> bool:
        return (
            math.hypot(
                self.north_slope_m_per_m,
                self.east_slope_m_per_m,
            )
            >= _NON_FLAT_SLOPE_MINIMUM
        )

    def elevation_msl(self, north_m: float, east_m: float) -> float:
        if not math.isfinite(north_m) or not math.isfinite(east_m):
            raise SimulatorTerrainTruthError("truth sample coordinate must be finite")
        east_translation, north_translation, up_translation, _roll, _pitch, _yaw = (
            self.model_pose_enu_rpy_rad
        )
        local_up = (
            up_translation
            + self.east_slope_m_per_m * (east_m - east_translation)
            + self.north_slope_m_per_m * (north_m - north_translation)
        )
        return self.origin_elevation_m + local_up

    def covers(self, north_m: float, east_m: float, *, tolerance_m: float = 1.0e-8) -> bool:
        """Return whether a point on the infinite truth plane lies inside its SDF rectangle."""

        elevation_local = self.elevation_msl(north_m, east_m) - self.origin_elevation_m
        translation = self.model_pose_enu_rpy_rad[:3]
        world_delta = (
            east_m - translation[0],
            north_m - translation[1],
            elevation_local - translation[2],
        )
        local = tuple(
            sum(self.rotation_enu_from_model[row][column] * world_delta[row] for row in range(3))
            for column in range(3)
        )
        return (
            abs(local[2]) <= tolerance_m
            and abs(local[0]) <= self.plane_size_m[0] / 2.0 + tolerance_m
            and abs(local[1]) <= self.plane_size_m[1] / 2.0 + tolerance_m
        )

    def intersect_ray(
        self,
        *,
        reference_north_m: float,
        reference_east_m: float,
        height_m: float,
        camera_offset_ned: tuple[float, float, float],
        ray_ned: tuple[float, float, float],
        maximum_slant_range_m: float,
        minimum_incidence_component: float,
    ) -> TerrainTruthRayIntersection | None:
        """Intersect one NED ray with the analytic SDF plane, without reading the DEM."""

        if len(camera_offset_ned) != 3 or len(ray_ned) != 3:
            return None
        numeric = (
            reference_north_m,
            reference_east_m,
            height_m,
            *camera_offset_ned,
            *ray_ned,
            maximum_slant_range_m,
            minimum_incidence_component,
        )
        if not all(math.isfinite(value) for value in numeric):
            return None
        if height_m <= 0.0 or maximum_slant_range_m <= 0.0:
            return None
        if minimum_incidence_component <= 0.0:
            return None
        ray_norm = math.sqrt(sum(value * value for value in ray_ned))
        if not math.isclose(ray_norm, 1.0, rel_tol=1.0e-6, abs_tol=1.0e-6):
            return None
        ray_north, ray_east, ray_down = ray_ned
        if ray_down <= 0.0:
            return None

        camera_north = reference_north_m + camera_offset_ned[0]
        camera_east = reference_east_m + camera_offset_ned[1]
        if not self.covers(reference_north_m, reference_east_m) or not self.covers(
            camera_north,
            camera_east,
        ):
            return None
        reference_surface = self.elevation_msl(reference_north_m, reference_east_m)
        camera_altitude = reference_surface + height_m - camera_offset_ned[2]
        camera_surface = self.elevation_msl(camera_north, camera_east)
        clearance = camera_altitude - camera_surface
        incidence = (
            ray_down + self.north_slope_m_per_m * ray_north + self.east_slope_m_per_m * ray_east
        )
        if clearance <= 0.0 or incidence < minimum_incidence_component:
            return None
        slant_range = clearance / incidence
        if slant_range <= 0.0 or slant_range > maximum_slant_range_m:
            return None
        north_m = camera_north + slant_range * ray_north
        east_m = camera_east + slant_range * ray_east
        if not self.covers(north_m, east_m):
            return None
        return TerrainTruthRayIntersection(
            slant_range_m=slant_range,
            north_m=north_m,
            east_m=east_m,
            elevation_m=self.elevation_msl(north_m, east_m),
            incidence_component=incidence,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "sdf_version": self.sdf_version,
            "world_name": self.world_name,
            "surface_model": self.surface_model,
            "world_frame_orientation": self.world_frame_orientation,
            "origin": {
                "latitude_deg": self.origin_latitude_deg,
                "longitude_deg": self.origin_longitude_deg,
                "elevation_m": self.origin_elevation_m,
            },
            "model_name": self.model_name,
            "model_pose_enu_rpy_rad": list(self.model_pose_enu_rpy_rad),
            "model_normal_enu": list(self.model_normal_enu),
            "plane_size_m": list(self.plane_size_m),
            "north_slope_m_per_m": self.north_slope_m_per_m,
            "east_slope_m_per_m": self.east_slope_m_per_m,
            "surface_elevation_at_local_zero_msl": self.surface_elevation_at_local_zero_msl,
        }


@dataclass(frozen=True, slots=True)
class TerrainRayProbe:
    probe_id: str
    reference_north_m: float
    reference_east_m: float
    height_m: float
    camera_offset_ned: tuple[float, float, float]
    ray_ned: tuple[float, float, float]
    maximum_slant_range_m: float = 400.0
    minimum_incidence_component: float = 0.05

    def __post_init__(self) -> None:
        if not self.probe_id.strip():
            raise SimulatorTerrainTruthError("terrain ray probe id cannot be empty")
        values = (
            self.reference_north_m,
            self.reference_east_m,
            self.height_m,
            *self.camera_offset_ned,
            *self.ray_ned,
            self.maximum_slant_range_m,
            self.minimum_incidence_component,
        )
        if not all(math.isfinite(value) for value in values):
            raise SimulatorTerrainTruthError("terrain ray probe values must be finite")
        if self.height_m <= 0.0 or self.maximum_slant_range_m <= 0.0:
            raise SimulatorTerrainTruthError("terrain ray probe range values must be positive")
        if self.minimum_incidence_component <= 0.0:
            raise SimulatorTerrainTruthError("terrain ray probe incidence gate must be positive")
        norm = math.sqrt(sum(value * value for value in self.ray_ned))
        if not math.isclose(norm, 1.0, rel_tol=0.0, abs_tol=1.0e-12):
            raise SimulatorTerrainTruthError("terrain ray probe direction must be unit length")


def _unit_ned(north: float, east: float, down: float) -> tuple[float, float, float]:
    norm = math.sqrt(north * north + east * east + down * down)
    return north / norm, east / norm, down / norm


DEFAULT_RAY_PROBES: tuple[TerrainRayProbe, ...] = (
    TerrainRayProbe("nadir-origin", 0.0, 0.0, 80.0, (1.2, -0.7, 0.4), (0.0, 0.0, 1.0)),
    TerrainRayProbe(
        "southwest-to-northeast",
        -160.0,
        -150.0,
        120.0,
        (1.0, 0.5, 0.2),
        _unit_ned(0.72, 0.28, 1.0),
    ),
    TerrainRayProbe(
        "northeast-to-southwest",
        160.0,
        150.0,
        125.0,
        (-0.5, -1.0, -0.2),
        _unit_ned(-0.75, -0.20, 1.0),
    ),
    TerrainRayProbe(
        "east-to-west",
        -50.0,
        160.0,
        100.0,
        (0.4, -0.8, 0.1),
        _unit_ned(0.40, -0.80, 1.0),
    ),
    TerrainRayProbe(
        "west-to-east",
        120.0,
        -160.0,
        110.0,
        (-0.6, 0.9, -0.3),
        _unit_ned(-0.35, 0.90, 1.0),
    ),
    TerrainRayProbe(
        "south-long-oblique",
        -180.0,
        20.0,
        130.0,
        (0.3, 0.2, 0.0),
        _unit_ned(0.90, 0.05, 1.0),
    ),
    TerrainRayProbe(
        "north-long-oblique",
        180.0,
        -20.0,
        130.0,
        (-0.3, -0.2, 0.0),
        _unit_ned(-0.90, -0.05, 1.0),
    ),
)


def load_sdf_terrain_truth(path: str | Path) -> TerrainTruthPlane:
    """Load a strict, self-contained SDF plane and derive its ENU geometry."""

    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise SimulatorTerrainTruthError(f"terrain truth SDF is not a regular file: {source}")
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise SimulatorTerrainTruthError(f"terrain truth SDF cannot be read: {source}") from exc
    if not payload or len(payload) > _MAX_SDF_BYTES:
        raise SimulatorTerrainTruthError("terrain truth SDF size is outside supported bounds")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SimulatorTerrainTruthError("terrain truth SDF is not valid UTF-8") from exc
    if _FORBIDDEN_XML_DECLARATION_RE.search(text):
        raise SimulatorTerrainTruthError("terrain truth SDF cannot contain DTD/entity declarations")
    if _NETWORK_OR_EXTERNAL_URI_RE.search(text):
        raise SimulatorTerrainTruthError("terrain truth SDF cannot contain external asset URIs")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise SimulatorTerrainTruthError("terrain truth SDF is not valid XML") from exc
    if root.tag != "sdf" or set(root.attrib) != {"version"}:
        raise SimulatorTerrainTruthError("terrain truth must have one <sdf version=...> root")
    sdf_version = root.attrib["version"].strip()
    if not sdf_version:
        raise SimulatorTerrainTruthError("terrain truth SDF version cannot be empty")
    worlds = root.findall("world")
    if len(worlds) != 1 or worlds[0].attrib != {"name": _ROOT_NAME}:
        raise SimulatorTerrainTruthError("terrain truth SDF must define exactly one default world")
    world = worlds[0]
    if world.findall(".//include"):
        raise SimulatorTerrainTruthError("terrain truth SDF must be self-contained")

    spherical_items = world.findall("spherical_coordinates")
    if len(spherical_items) != 1:
        raise SimulatorTerrainTruthError("terrain truth SDF requires one spherical_coordinates")
    spherical = spherical_items[0]
    surface_model = _required_text(spherical, "surface_model")
    orientation = _required_text(spherical, "world_frame_orientation")
    latitude = _required_float(spherical, "latitude_deg")
    longitude = _required_float(spherical, "longitude_deg")
    elevation = _required_float(spherical, "elevation")
    if surface_model != "EARTH_WGS84" or orientation != "ENU":
        raise SimulatorTerrainTruthError("terrain truth SDF must use EARTH_WGS84 with ENU")
    if not -90.0 < latitude < 90.0 or not -180.0 <= longitude <= 180.0:
        raise SimulatorTerrainTruthError("terrain truth SDF WGS-84 origin is invalid")
    if not -500.0 <= elevation <= 9_000.0:
        raise SimulatorTerrainTruthError("terrain truth SDF origin elevation is invalid")

    models = [
        model for model in world.findall("model") if model.attrib.get("name") == _TRUTH_MODEL_NAME
    ]
    if len(models) != 1 or models[0].attrib != {"name": _TRUTH_MODEL_NAME}:
        raise SimulatorTerrainTruthError(
            "terrain truth SDF requires one uniquely named plane model"
        )
    model = models[0]
    if _required_text(model, "static").lower() != "true":
        raise SimulatorTerrainTruthError("terrain truth plane must be static")
    model_poses = model.findall("pose")
    if len(model_poses) != 1 or model_poses[0].attrib:
        raise SimulatorTerrainTruthError("terrain truth plane requires one world-frame model pose")
    pose = _parse_vector(model_poses[0].text, length=6, label="truth model pose")
    extra_poses = [item for item in model.findall(".//pose") if item is not model_poses[0]]
    if extra_poses:
        raise SimulatorTerrainTruthError(
            "terrain truth plane cannot contain nested pose transforms"
        )

    links = model.findall("link")
    if len(links) != 1:
        raise SimulatorTerrainTruthError("terrain truth plane must contain exactly one link")
    collisions = links[0].findall("collision")
    visuals = links[0].findall("visual")
    if len(collisions) != 1 or len(visuals) != 1:
        raise SimulatorTerrainTruthError(
            "terrain truth plane must contain exactly one collision and one visual"
        )
    collision_geometry = collisions[0].findall("geometry")
    visual_geometry = visuals[0].findall("geometry")
    if len(collision_geometry) != 1 or len(visual_geometry) != 1:
        raise SimulatorTerrainTruthError(
            "terrain truth collision and visual require one geometry each"
        )
    collision_planes = collision_geometry[0].findall("plane")
    visual_planes = visual_geometry[0].findall("plane")
    if (
        len(collision_planes) != 1
        or len(visual_planes) != 1
        or len(list(collision_geometry[0])) != 1
        or len(list(visual_geometry[0])) != 1
    ):
        raise SimulatorTerrainTruthError(
            "terrain truth requires one collision and one visual plane"
        )
    collision_normal = _parse_vector(
        collision_planes[0].findtext("normal"),
        length=3,
        label="truth collision plane normal",
    )
    visual_normal = _parse_vector(
        visual_planes[0].findtext("normal"),
        length=3,
        label="truth visual plane normal",
    )
    expected_normal = (0.0, 0.0, 1.0)
    if not _vectors_close(collision_normal, expected_normal) or not _vectors_close(
        visual_normal,
        expected_normal,
    ):
        raise SimulatorTerrainTruthError("truth plane local normal must be 0 0 1")
    collision_size = _parse_vector(
        collision_planes[0].findtext("size"),
        length=2,
        label="truth collision plane size",
    )
    visual_size = _parse_vector(
        visual_planes[0].findtext("size"),
        length=2,
        label="truth visual plane size",
    )
    if not _vectors_close(collision_size, visual_size):
        raise SimulatorTerrainTruthError("truth collision and visual plane sizes must match")
    if any(value <= 0.0 or value > 1_000_000.0 for value in collision_size):
        raise SimulatorTerrainTruthError("truth plane size is outside supported bounds")

    rotation = _rotation_matrix(roll=pose[3], pitch=pose[4], yaw=pose[5])
    normal = (rotation[0][2], rotation[1][2], rotation[2][2])
    return TerrainTruthPlane(
        scenario_id=source.stem,
        sdf_version=sdf_version,
        world_name=_ROOT_NAME,
        surface_model=surface_model,
        world_frame_orientation=orientation,
        origin_latitude_deg=latitude,
        origin_longitude_deg=longitude,
        origin_elevation_m=elevation,
        model_name=_TRUTH_MODEL_NAME,
        model_pose_enu_rpy_rad=pose,
        model_normal_enu=normal,
        plane_size_m=collision_size,
        rotation_enu_from_model=rotation,
        source_sha256=hashlib.sha256(payload).hexdigest(),
        source_bytes=len(payload),
    )


def compare_dem_to_sdf_truth(
    grid: LocalDemGrid,
    truth: TerrainTruthPlane,
    *,
    probes: Sequence[TerrainRayProbe] = DEFAULT_RAY_PROBES,
    tolerances: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Compare DEM samples and intersections with independently parsed SDF truth."""

    if not isinstance(grid, LocalDemGrid) or not isinstance(truth, TerrainTruthPlane):
        raise TypeError("terrain comparison requires a LocalDemGrid and TerrainTruthPlane")
    selected_tolerances = dict(DEFAULT_TOLERANCES if tolerances is None else tolerances)
    if set(selected_tolerances) != set(DEFAULT_TOLERANCES):
        raise SimulatorTerrainTruthError("terrain comparison tolerance fields are invalid")
    if any(
        isinstance(value, bool) or not math.isfinite(value) or value <= 0.0
        for value in selected_tolerances.values()
    ):
        raise SimulatorTerrainTruthError(
            "terrain comparison tolerances must be finite and positive"
        )
    selected_probes = tuple(probes)
    if not 5 <= len(selected_probes) <= 7:
        raise SimulatorTerrainTruthError(
            "terrain truth acceptance requires five to seven ray probes"
        )
    if len({probe.probe_id for probe in selected_probes}) != len(selected_probes):
        raise SimulatorTerrainTruthError("terrain truth ray probe ids must be unique")

    checks: dict[str, bool] = {}
    reasons: list[str] = []

    def check(name: str, condition: bool, reason: str) -> None:
        checks[name] = bool(condition)
        if not condition:
            reasons.append(reason)

    check(
        "scenario_identity_matches",
        grid.scenario_id == truth.scenario_id,
        "DEM scenario_id does not match the SDF filename identity",
    )
    world_origin_matches = (
        abs(grid.origin_latitude_deg - truth.origin_latitude_deg) <= _ORIGIN_ANGLE_TOLERANCE_DEG
        and abs(grid.origin_longitude_deg - truth.origin_longitude_deg)
        <= _ORIGIN_ANGLE_TOLERANCE_DEG
        and abs(grid.origin_elevation_m - truth.origin_elevation_m) <= _ORIGIN_ELEVATION_TOLERANCE_M
    )
    check(
        "world_origin_matches_dem",
        world_origin_matches,
        "DEM WGS-84/MSL origin does not match SDF spherical_coordinates",
    )
    check(
        "surface_origin_matches_dem",
        abs(grid.origin_elevation_m - truth.surface_elevation_at_local_zero_msl)
        <= selected_tolerances["elevation_m"],
        "DEM elevation at local zero does not match SDF plane truth",
    )
    check(
        "truth_surface_is_non_flat",
        truth.is_non_flat,
        "SDF truth surface is flat and cannot close the slope subgate",
    )

    sample_points: list[tuple[str, float, float]] = []
    for row in range(grid.row_count):
        north = grid.north_origin_m + row * grid.north_spacing_m
        for column in range(grid.column_count):
            east = grid.east_origin_m + column * grid.east_spacing_m
            sample_points.append(("node", north, east))
    node_sample_count = len(sample_points)
    for row in range(grid.row_count - 1):
        north = grid.north_origin_m + (row + 0.5) * grid.north_spacing_m
        for column in range(grid.column_count - 1):
            east = grid.east_origin_m + (column + 0.5) * grid.east_spacing_m
            sample_points.append(("cell_center", north, east))
    cell_center_sample_count = len(sample_points) - node_sample_count

    elevation_errors: list[float] = []
    north_slope_errors: list[float] = []
    east_slope_errors: list[float] = []
    coverage = True
    for _kind, north, east in sample_points:
        coverage = coverage and truth.covers(north, east)
        sample = grid.sample_local(north, east)
        elevation_errors.append(abs(sample.elevation_m - truth.elevation_msl(north, east)))
        north_slope_errors.append(abs(sample.north_slope_m_per_m - truth.north_slope_m_per_m))
        east_slope_errors.append(abs(sample.east_slope_m_per_m - truth.east_slope_m_per_m))
    max_elevation_error = max(elevation_errors)
    max_north_slope_error = max(north_slope_errors)
    max_east_slope_error = max(east_slope_errors)
    check(
        "truth_plane_covers_dem",
        coverage,
        "finite SDF plane does not cover every DEM node and cell center",
    )
    check(
        "all_grid_samples_compared",
        len(elevation_errors) == node_sample_count + cell_center_sample_count,
        "not every DEM node and cell center was compared",
    )
    check(
        "dem_elevation_within_tolerance",
        max_elevation_error <= selected_tolerances["elevation_m"],
        "DEM elevation differs from analytic SDF truth",
    )
    check(
        "dem_slope_within_tolerance",
        max(max_north_slope_error, max_east_slope_error) <= selected_tolerances["slope_m_per_m"],
        "DEM gradient differs from analytic SDF truth",
    )

    ray_records: list[dict[str, Any]] = []
    all_rays_intersect = True
    all_rays_within_tolerance = True
    cross_cell_ray_probe_count = 0
    error_names = (
        "slant_range_m",
        "north_m",
        "east_m",
        "elevation_m",
        "incidence_component",
    )
    ray_error_values: dict[str, list[float]] = {name: [] for name in error_names}
    for probe in selected_probes:
        truth_hit = truth.intersect_ray(
            reference_north_m=probe.reference_north_m,
            reference_east_m=probe.reference_east_m,
            height_m=probe.height_m,
            camera_offset_ned=probe.camera_offset_ned,
            ray_ned=probe.ray_ned,
            maximum_slant_range_m=probe.maximum_slant_range_m,
            minimum_incidence_component=probe.minimum_incidence_component,
        )
        dem_hit = grid.intersect_ray(
            reference_north_m=probe.reference_north_m,
            reference_east_m=probe.reference_east_m,
            height_m=probe.height_m,
            camera_offset_ned=probe.camera_offset_ned,
            ray_ned=probe.ray_ned,
            maximum_slant_range_m=probe.maximum_slant_range_m,
            minimum_incidence_component=probe.minimum_incidence_component,
        )
        intersected = truth_hit is not None and dem_hit is not None
        all_rays_intersect = all_rays_intersect and intersected
        absolute_error: dict[str, float] | None = None
        traversed_cell_count: int | None = None
        within_tolerance = False
        if truth_hit is not None and dem_hit is not None:
            absolute_error = _intersection_errors(dem_hit, truth_hit)
            traversed_cell_count = _traversed_cell_count(
                grid,
                start_north_m=probe.reference_north_m + probe.camera_offset_ned[0],
                start_east_m=probe.reference_east_m + probe.camera_offset_ned[1],
                end_north_m=dem_hit.north_m,
                end_east_m=dem_hit.east_m,
            )
            if traversed_cell_count >= 2:
                cross_cell_ray_probe_count += 1
            for name, value in absolute_error.items():
                ray_error_values[name].append(value)
            within_tolerance = all(
                absolute_error[name] <= selected_tolerances[f"ray_{name}"] for name in error_names
            )
        all_rays_within_tolerance = all_rays_within_tolerance and within_tolerance
        ray_records.append(
            {
                "probe_id": probe.probe_id,
                "reference_north_m": probe.reference_north_m,
                "reference_east_m": probe.reference_east_m,
                "height_m": probe.height_m,
                "camera_offset_ned": list(probe.camera_offset_ned),
                "ray_ned": list(probe.ray_ned),
                "truth": truth_hit.to_mapping() if truth_hit is not None else None,
                "dem": _dem_hit_mapping(dem_hit) if dem_hit is not None else None,
                "absolute_error": absolute_error,
                "traversed_cell_count": traversed_cell_count,
                "gate_passed": intersected and within_tolerance,
            }
        )
    check(
        "all_ray_probes_intersect",
        all_rays_intersect,
        "one or more deterministic rays did not intersect both SDF truth and DEM",
    )
    check(
        "all_ray_probes_within_tolerance",
        all_rays_within_tolerance,
        "one or more DEM ray intersections differ from analytic SDF truth",
    )
    check(
        "at_least_five_cross_cell_rays",
        cross_cell_ray_probe_count >= 5,
        "fewer than five deterministic rays cross a DEM cell boundary",
    )

    max_ray_errors: dict[str, float | None] = {
        name: max(values) if values else None for name, values in ray_error_values.items()
    }
    gate_passed = all(checks.values())
    return {
        "sample_method": "all DEM nodes plus all bilinear cell centers",
        "node_sample_count": node_sample_count,
        "cell_center_sample_count": cell_center_sample_count,
        "total_sample_count": len(sample_points),
        "ray_probe_count": len(selected_probes),
        "cross_cell_ray_probe_count": cross_cell_ray_probe_count,
        "tolerances": selected_tolerances,
        "max_errors": {
            "elevation_m": max_elevation_error,
            "north_slope_m_per_m": max_north_slope_error,
            "east_slope_m_per_m": max_east_slope_error,
            "ray_slant_range_m": max_ray_errors["slant_range_m"],
            "ray_north_m": max_ray_errors["north_m"],
            "ray_east_m": max_ray_errors["east_m"],
            "ray_elevation_m": max_ray_errors["elevation_m"],
            "ray_incidence_component": max_ray_errors["incidence_component"],
        },
        "rays": ray_records,
        "checks": checks,
        "failure_reasons": reasons,
        "gate_passed": gate_passed,
    }


def _required_text(parent: ET.Element, tag: str) -> str:
    items = parent.findall(tag)
    if len(items) != 1 or items[0].attrib:
        raise SimulatorTerrainTruthError(f"terrain truth requires one plain <{tag}> value")
    value = (items[0].text or "").strip()
    if not value:
        raise SimulatorTerrainTruthError(f"terrain truth <{tag}> cannot be empty")
    return value


def _required_float(parent: ET.Element, tag: str) -> float:
    text = _required_text(parent, tag)
    try:
        value = float(text)
    except ValueError as exc:
        raise SimulatorTerrainTruthError(f"terrain truth <{tag}> must be numeric") from exc
    if not math.isfinite(value):
        raise SimulatorTerrainTruthError(f"terrain truth <{tag}> must be finite")
    return value


def _parse_vector(text: str | None, *, length: int, label: str) -> tuple[float, ...]:
    if text is None:
        raise SimulatorTerrainTruthError(f"{label} is missing")
    try:
        values = tuple(float(item) for item in text.split())
    except ValueError as exc:
        raise SimulatorTerrainTruthError(f"{label} must contain numeric values") from exc
    if len(values) != length or not all(math.isfinite(value) for value in values):
        raise SimulatorTerrainTruthError(f"{label} must contain {length} finite values")
    return values


def _vectors_close(left: Sequence[float], right: Sequence[float]) -> bool:
    return len(left) == len(right) and all(
        math.isclose(first, second, rel_tol=0.0, abs_tol=_VECTOR_TOLERANCE)
        for first, second in zip(left, right, strict=True)
    )


def _rotation_matrix(
    *,
    roll: float,
    pitch: float,
    yaw: float,
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]:
    """Return Rz(yaw) * Ry(pitch) * Rx(roll) for SDF ENU poses."""

    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
        (-sp, cp * sr, cp * cr),
    )


def _intersection_errors(
    dem: DemRayIntersection,
    truth: TerrainTruthRayIntersection,
) -> dict[str, float]:
    return {
        "slant_range_m": abs(dem.slant_range_m - truth.slant_range_m),
        "north_m": abs(dem.north_m - truth.north_m),
        "east_m": abs(dem.east_m - truth.east_m),
        "elevation_m": abs(dem.elevation_m - truth.elevation_m),
        "incidence_component": abs(dem.incidence_component - truth.incidence_component),
    }


def _dem_hit_mapping(hit: DemRayIntersection) -> dict[str, float]:
    return {
        "slant_range_m": hit.slant_range_m,
        "north_m": hit.north_m,
        "east_m": hit.east_m,
        "elevation_m": hit.elevation_m,
        "incidence_component": hit.incidence_component,
    }


def _traversed_cell_count(
    grid: LocalDemGrid,
    *,
    start_north_m: float,
    start_east_m: float,
    end_north_m: float,
    end_east_m: float,
) -> int:
    """Count bilinear DEM cells touched by a horizontal ray segment."""

    parameters: list[float] = []
    for start, end, origin, spacing, count in (
        (
            start_north_m,
            end_north_m,
            grid.north_origin_m,
            grid.north_spacing_m,
            grid.row_count,
        ),
        (
            start_east_m,
            end_east_m,
            grid.east_origin_m,
            grid.east_spacing_m,
            grid.column_count,
        ),
    ):
        delta = end - start
        if abs(delta) <= 1.0e-15:
            continue
        for index in range(1, count - 1):
            boundary = origin + index * spacing
            parameter = (boundary - start) / delta
            if 1.0e-10 < parameter < 1.0 - 1.0e-10:
                parameters.append(parameter)
    parameters.sort()
    unique: list[float] = []
    for parameter in parameters:
        if not unique or abs(parameter - unique[-1]) > 1.0e-10:
            unique.append(parameter)
    return len(unique) + 1


__all__ = [
    "DEFAULT_RAY_PROBES",
    "DEFAULT_TOLERANCES",
    "SimulatorTerrainTruthError",
    "TerrainRayProbe",
    "TerrainTruthPlane",
    "TerrainTruthRayIntersection",
    "compare_dem_to_sdf_truth",
    "load_sdf_terrain_truth",
]
