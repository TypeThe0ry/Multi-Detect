"""Bounded static-SDF collision checks for an explicit SIM teacher route.

Only collision primitives participate; visual target identity/colour is not
exported to the actor. This is neither a live obstacle sensor nor flight
permission. Unsupported frames, moving bodies, plugins, meshes and unresolved
includes are rejected rather than silently treated as empty space.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..target_geolocation import WGS84_FLATTENING, WGS84_SEMI_MAJOR_AXIS_M
from .onboard_executed_trajectory import EvidenceBinding
from .serialization import (
    AutonomyContractError,
    canonical_json,
    canonical_sha256,
    require_array,
    require_bool,
    require_real,
    require_schema_version,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)

if TYPE_CHECKING:
    from ..px4_external_mission import ExternalRuntimeAttestation
    from ..px4_mission import MultirotorMissionGeometry
    from .contracts import ContextSnapshot
    from .onboard_teacher_mission import HeldTeacherLeg

MAXIMUM_SDF_BYTES = 1024 * 1024
MAXIMUM_COLLISIONS = 1024
MAXIMUM_MODELS = 128
_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_URI = re.compile(r"^model://([A-Za-z0-9_.-]{1,128})$")
_SDF_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_ZERO = (0.0, 0.0, 0.0)
_IDENTITY = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
ORIGIN_CONTRACT = "multidetect.explicit-SIM-origin-binding-v1"
ENVELOPE_CONTRACT = "multidetect.explicit-SIM-airframe-clearance-envelope-v1"
ROUTE_CONTRACT = "multidetect.static-SIM-teacher-route-clearance-v1"
MAXIMUM_INPUT_BYTES = 128 * 1024
MAXIMUM_AGE_S = 2.0
_ORIGIN_VERIFICATION_FIELDS = frozenset(
    {
        "PX4_navigation_reference_point_identified",
        "Gazebo_world_ENU_pose_observed",
        "pose_to_navigation_reference_extrinsic_bound",
        "EKF_reset_continuity_verified",
        "WGS84_home_mapping_checked",
    }
)


@dataclass(frozen=True, slots=True)
class StaticCollision:
    collision_id: str
    kind: str
    center_enu_m: tuple[float, float, float]
    rotation: tuple[tuple[float, float, float], ...]
    half_extents_local_m: tuple[float, float, float]
    minimum_enu_m: tuple[float, float, float] | None
    maximum_enu_m: tuple[float, float, float] | None
    source_sha256: str

    @property
    def upper_surface_up_m(self) -> float:
        return self.center_enu_m[2] if self.kind == "plane" else self.maximum_enu_m[2]

    def to_mapping(self) -> dict[str, object]:
        return {
            "collision_id": self.collision_id,
            "kind": self.kind,
            "center_enu_m": list(self.center_enu_m),
            "rotation": [list(row) for row in self.rotation],
            "half_extents_local_m": list(self.half_extents_local_m),
            "minimum_enu_m": None if self.minimum_enu_m is None else list(self.minimum_enu_m),
            "maximum_enu_m": None if self.maximum_enu_m is None else list(self.maximum_enu_m),
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True, slots=True)
class StaticGazeboScene:
    world_source: EvidenceBinding
    include_sources: tuple[tuple[str, EvidenceBinding], ...]
    world_name: str
    reference_latitude_deg: float
    reference_longitude_deg: float
    reference_elevation_m: float
    collisions: tuple[StaticCollision, ...]

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    def to_mapping(self) -> dict[str, object]:
        return {
            "contract": "multidetect.static-Gazebo-collision-scene-v1",
            "schema_version": 1,
            "world_source": self.world_source.to_mapping(),
            "include_sources": {uri: source.to_mapping() for uri, source in self.include_sources},
            "world_name": self.world_name,
            "world_frame": "ENU",
            "surface_model": "EARTH_WGS84",
            "reference_latitude_deg": self.reference_latitude_deg,
            "reference_longitude_deg": self.reference_longitude_deg,
            "reference_elevation_m": self.reference_elevation_m,
            "collisions": [collision.to_mapping() for collision in self.collisions],
            "scope": "static collision primitives only; not dynamic/runtime/flight acceptance",
        }


def _read_bound_sdf(root: Path, source: EvidenceBinding, identities: dict) -> ET.Element:
    if type(source) is not EvidenceBinding or source.json_pointer is not None:
        raise AutonomyContractError("SDF source must be an exact whole-file EvidenceBinding")
    if source.bytes > MAXIMUM_SDF_BYTES:
        raise AutonomyContractError("SDF source exceeds the bounded read cap")
    path = root / source.path
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
        raise AutonomyContractError("SDF source must be a contained regular file")
    before = path.stat()
    with path.open("rb") as stream:
        payload = stream.read(MAXIMUM_SDF_BYTES + 1)
    after = path.stat()

    def identity(stat):
        return stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino

    if (
        identity(before) != identity(after)
        or len(payload) != source.bytes
        or hashlib.sha256(payload).hexdigest() != source.sha256
    ):
        raise AutonomyContractError("SDF source identity/bytes/SHA differ")
    prior = identities.setdefault(source.path, identity(after))
    if prior != identity(after):
        raise AutonomyContractError("SDF source changed during collision inspection")
    try:
        text = payload.decode("utf-8-sig")
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
            raise AutonomyContractError("SDF entities and DTDs are unsupported")
        result = ET.fromstring(text)
    except (UnicodeDecodeError, ET.ParseError) as exc:
        raise AutonomyContractError("SDF source is not supported UTF-8 XML") from exc
    if result.tag != "sdf" or result.attrib != {"version": "1.9"}:
        raise AutonomyContractError("collision inspection supports plain SDF 1.9 only")
    return result


def _one(parent: ET.Element, tag: str, *, optional: bool = False) -> ET.Element | None:
    values = parent.findall(tag)
    if len(values) != 1:
        if optional and not values:
            return None
        raise AutonomyContractError(f"{parent.tag} requires exactly one {tag}")
    return values[0]


def _vector(value: object, count: int, name: str) -> tuple[float, ...]:
    if not isinstance(value, str) or not value.isascii():
        raise AutonomyContractError(f"{name} must be numeric SDF text")
    # Python float accepts underscores and Unicode decimal digits that a
    # Gazebo/C++ SDF parser need not accept. XML numeric separators are only
    # ASCII space/tab/CR/LF, with decimal/scientific ASCII component syntax.
    tokens = re.split(r"[ \t\r\n]+", value.strip(" \t\r\n"))
    if len(tokens) != count or any(_SDF_NUMBER.fullmatch(token) is None for token in tokens):
        raise AutonomyContractError(f"{name} requires exact ASCII SDF numeric syntax")
    try:
        values = tuple(float(item) for item in tokens)
    except ValueError as exc:
        raise AutonomyContractError(f"{name} is not numeric") from exc
    if len(values) != count or not all(math.isfinite(v) and abs(v) <= 100000 for v in values):
        raise AutonomyContractError(f"{name} vector is outside supported bounds")
    return values


def _name(element: ET.Element) -> str:
    value = element.attrib.get("name")
    if value is None or _ID.fullmatch(value) is None:
        raise AutonomyContractError("collision hierarchy needs bounded unambiguous names")
    return value


def _mv(rotation, vector):
    return tuple(sum(rotation[i][j] * vector[j] for j in range(3)) for i in range(3))


def _compose(parent, local):
    pr, pt = parent
    lr, lt = local
    rotation = tuple(
        tuple(sum(pr[i][k] * lr[k][j] for k in range(3)) for j in range(3)) for i in range(3)
    )
    offset = _mv(pr, lt)
    return rotation, tuple(pt[i] + offset[i] for i in range(3))


def _pose(element):
    pose = _one(element, "pose", optional=True)
    if pose is None:
        return _IDENTITY, _ZERO
    if pose.attrib:
        raise AutonomyContractError("relative_to/degrees/quaternion SDF poses are unsupported")
    x, y, z, roll, pitch, yaw = _vector(pose.text, 6, "pose")
    cr, sr, cp, sp, cy, sy = (
        math.cos(roll),
        math.sin(roll),
        math.cos(pitch),
        math.sin(pitch),
        math.cos(yaw),
        math.sin(yaw),
    )
    rotation = (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
        (-sp, cp * sr, cp * cr),
    )
    return rotation, (x, y, z)


def _collision(element, parent_pose, collision_id, source_sha256):
    pose = _compose(parent_pose, _pose(element))
    geometry = _one(element, "geometry")
    children = list(geometry)
    if len(children) != 1 or geometry.attrib:
        raise AutonomyContractError("collision requires one plain primitive geometry")
    shape = children[0]
    rotation, center = pose
    if shape.attrib:
        raise AutonomyContractError("collision primitive attributes are unsupported")
    fields = {
        "plane": {"normal", "size"},
        "box": {"size"},
        "cylinder": {"radius", "length"},
        "sphere": {"radius"},
    }
    if shape.tag not in fields or any(child.tag not in fields[shape.tag] for child in shape):
        raise AutonomyContractError(f"unsupported collision geometry/fields {shape.tag!r}")
    if shape.tag == "plane":
        normal = _vector(_one(shape, "normal").text, 3, "plane normal")
        size = _vector(_one(shape, "size").text, 2, "plane size")
        if normal != (0, 0, 1) or any(v <= 0 for v in size):
            raise AutonomyContractError("only positive-size upward collision planes are supported")
        world_normal = _mv(rotation, normal)
        if max(abs(world_normal[i] - (0, 0, 1)[i]) for i in range(3)) > 1e-10:
            raise AutonomyContractError(
                "sloped collision planes require a different clearance model"
            )
        half = (size[0] / 2, size[1] / 2, 0.0)
        bounds = (None, None)  # conservatively an infinite floor, never ignored in cruise
    elif shape.tag == "box":
        size = _vector(_one(shape, "size").text, 3, "box size")
        half = tuple(v / 2 for v in size)
        bounds = None
    elif shape.tag == "cylinder":
        radius = _vector(_one(shape, "radius").text, 1, "cylinder radius")[0]
        length = _vector(_one(shape, "length").text, 1, "cylinder length")[0]
        half = (radius, radius, length / 2)
        bounds = None
    elif shape.tag == "sphere":
        radius = _vector(_one(shape, "radius").text, 1, "sphere radius")[0]
        half = (radius, radius, radius)
        bounds = None
    else:
        raise AutonomyContractError(f"unsupported collision geometry {shape.tag!r}")
    if shape.tag != "plane" and any(v <= 0 for v in half):
        raise AutonomyContractError("collision primitive dimensions must be positive")
    if bounds is None:
        corners = [
            _mv(rotation, tuple(sign[i] * half[i] for i in range(3)))
            for sign in itertools.product((-1, 1), repeat=3)
        ]
        bounds = (
            tuple(center[i] + min(corner[i] for corner in corners) for i in range(3)),
            tuple(center[i] + max(corner[i] for corner in corners) for i in range(3)),
        )
    return StaticCollision(collision_id, shape.tag, center, rotation, half, *bounds, source_sha256)


def _model_collisions(model, parent_pose, namespace, source_sha256):
    if model.attrib != {"name": _name(model)}:
        raise AutonomyContractError("static model attributes are unsupported")
    static = _one(model, "static")
    if static.text is None or static.text.strip() != "true":
        raise AutonomyContractError("moving models cannot use static route clearance")
    if any(child.tag not in {"static", "pose", "link"} for child in model):
        raise AutonomyContractError("nested models/includes/frames/joints/plugins are unsupported")
    pose = _compose(parent_pose, _pose(model))
    result = []
    names = set()
    for link in model.findall("link"):
        name = _name(link)
        if link.attrib != {"name": name} or name in names:
            raise AutonomyContractError("static links must have exact unique names")
        names.add(name)
        if any(
            child.tag
            not in {
                "pose",
                "collision",
                "visual",
                "inertial",
                "gravity",
                "self_collide",
                "kinematic",
            }
            for child in link
        ):
            raise AutonomyContractError("unsupported static-link frame or dynamic extension")
        for collision in link.findall("collision"):
            collision_name = _name(collision)
            if collision.attrib != {"name": collision_name}:
                raise AutonomyContractError("collision attributes are unsupported")
            result.append(
                _collision(
                    collision,
                    _compose(pose, _pose(link)),
                    f"{namespace}/{name}/{collision_name}",
                    source_sha256,
                )
            )
    return result


def inspect_static_gazebo_scene(
    *,
    artifact_root: str | Path,
    world_source: EvidenceBinding,
    include_model_sources: Mapping[str, EvidenceBinding],
) -> StaticGazeboScene:
    """Reopen one local world and every included collision model, then bound it.

    The exact includes must be supplied by the caller as content bindings;
    Gazebo resource-path lookups and remote Fuel resolution are not guessed.
    The vehicle's upstream composition is deliberately not parsed as a static
    obstacle or a certified envelope. Its separately qualified envelope is a
    route-check input.
    """
    root = Path(artifact_root).resolve()
    if not root.is_dir() or not isinstance(include_model_sources, Mapping):
        raise AutonomyContractError(
            "existing artifact_root and exact include bindings are required"
        )
    identities: dict = {}
    document = _read_bound_sdf(root, world_source, identities)
    if any(element.tag in {"plugin", "actor", "frame"} for element in document.iter()):
        raise AutonomyContractError("world nested dynamic extensions/frames are unsupported")
    if len(document) != 1 or document[0].tag != "world":
        raise AutonomyContractError("SDF source requires exactly one world")
    world = document[0]
    world_name = _name(world)
    if world.attrib != {"name": world_name}:
        raise AutonomyContractError("world attributes are unsupported")
    allowed = {
        "physics",
        "gravity",
        "magnetic_field",
        "atmosphere",
        "wind",
        "scene",
        "spherical_coordinates",
        "light",
        "model",
        "include",
    }
    if any(child.tag not in allowed for child in world):
        raise AutonomyContractError(
            "world actors/frames/plugins/dynamic extensions are unsupported"
        )
    spherical = _one(world, "spherical_coordinates")
    if (
        _one(spherical, "surface_model").text != "EARTH_WGS84"
        or _one(spherical, "world_frame_orientation").text != "ENU"
        or spherical.find("heading_deg") is not None
    ):
        raise AutonomyContractError("static clearance needs unrotated EARTH_WGS84 ENU reference")
    lat = _vector(_one(spherical, "latitude_deg").text, 1, "reference latitude")[0]
    lon = _vector(_one(spherical, "longitude_deg").text, 1, "reference longitude")[0]
    elevation = _vector(_one(spherical, "elevation").text, 1, "reference elevation")[0]
    if not -89 < lat < 89 or not -180 <= lon <= 180:
        raise AutonomyContractError("SDF geographic reference is outside supported local domain")
    if len(world.findall("model")) + len(world.findall("include")) > MAXIMUM_MODELS:
        raise AutonomyContractError("static world model count exceeds bounds")
    collisions, included, model_names = [], set(), set()
    for model in world.findall("model"):
        name = _name(model)
        if name in model_names:
            raise AutonomyContractError("world model names are duplicated")
        model_names.add(name)
        collisions.extend(_model_collisions(model, (_IDENTITY, _ZERO), name, world_source.sha256))
    for include in world.findall("include"):
        if include.attrib or any(child.tag not in {"uri", "name", "pose"} for child in include):
            raise AutonomyContractError("include merge/placement/static overrides are unsupported")
        uri = require_text(_one(include, "uri").text, "include URI")
        match = _URI.fullmatch(uri)
        if match is None or uri not in include_model_sources:
            raise AutonomyContractError(
                "included model is missing an exact local SDF source binding"
            )
        included.add(uri)
        model_document = _read_bound_sdf(root, include_model_sources[uri], identities)
        if any(element.tag in {"plugin", "actor", "frame"} for element in model_document.iter()):
            raise AutonomyContractError("included nested dynamic extensions/frames are unsupported")
        if (
            len(model_document) != 1
            or model_document[0].tag != "model"
            or _name(model_document[0]) != match[1]
        ):
            raise AutonomyContractError("included SDF model identity differs from its URI")
        model = model_document[0]
        if model.find("pose") is not None:
            raise AutonomyContractError(
                "included model root pose override semantics are unsupported"
            )
        name_element = _one(include, "name", optional=True)
        name = (
            match[1]
            if name_element is None
            else require_text(name_element.text, "included model name")
        )
        if _ID.fullmatch(name) is None or name in model_names:
            raise AutonomyContractError("included model instance names are duplicated or invalid")
        model_names.add(name)
        collisions.extend(
            _model_collisions(model, _pose(include), name, include_model_sources[uri].sha256)
        )
    if set(include_model_sources) != included:
        raise AutonomyContractError("extra include bindings do not belong to the world")
    if (
        not collisions
        or len(collisions) > MAXIMUM_COLLISIONS
        or len({c.collision_id for c in collisions}) != len(collisions)
    ):
        raise AutonomyContractError(
            "static collision count/identity is empty, duplicated or excessive"
        )
    _read_bound_sdf(root, world_source, identities)
    for source in include_model_sources.values():
        _read_bound_sdf(root, source, identities)
    return StaticGazeboScene(
        world_source,
        tuple(sorted(include_model_sources.items())),
        world_name,
        lat,
        lon,
        elevation,
        tuple(collisions),
    )


def _bound_bytes(root, source, cap, identities):
    if type(source) is not EvidenceBinding or source.bytes > cap:
        raise AutonomyContractError("source binding or bounded read size is invalid")
    path = root / source.path
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
        raise AutonomyContractError("clearance source must be a contained regular file")
    before = path.stat()
    with path.open("rb") as stream:
        raw = stream.read(cap + 1)
    after = path.stat()
    identity = (after.st_size, after.st_mtime_ns, after.st_dev, after.st_ino)
    if (
        (before.st_size, before.st_mtime_ns, before.st_dev, before.st_ino) != identity
        or len(raw) != source.bytes
        or hashlib.sha256(raw).hexdigest() != source.sha256
        or identities.setdefault(source.path, identity) != identity
    ):
        raise AutonomyContractError("clearance source identity/bytes/SHA differ")
    return raw


def _bound_json(root, source, identities):
    if type(source) is not EvidenceBinding or source.json_pointer is None:
        raise AutonomyContractError("clearance JSON source requires an explicit pointer")
    value = strict_json_loads(_bound_bytes(root, source, MAXIMUM_INPUT_BYTES, identities))
    for token in source.json_pointer.split("/")[1:] if source.json_pointer else ():
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, Mapping) and key in value:
            value = value[key]
        elif (
            type(value) is list
            and key.isdecimal()
            and str(int(key)) == key
            and int(key) < len(value)
        ):
            value = value[int(key)]
        else:
            raise AutonomyContractError("clearance JSON source pointer cannot be resolved")
    return value


@dataclass(frozen=True, slots=True)
class ExplicitSimOriginBinding:
    """Source-qualified caller's origin metadata, not an issuer signature.

    The capture/verification layer must establish the named pose, navigation
    reference extrinsic and reset continuity from actual same-window sources.
    This loader checks exact metadata/source bindings, not independent packet
    semantics. Retained R2 lacks that evidence and cannot yield this metadata.
    """

    _document_json: str

    @classmethod
    def from_mapping(cls, raw: object) -> ExplicitSimOriginBinding:
        value = strict_object(
            raw,
            name="explicit SIM origin",
            required=frozenset(
                {
                    "contract",
                    "schema_version",
                    "runtime_kind",
                    "vehicle_class",
                    "observed_context_sha256",
                    "airframe_profile_id",
                    "airframe_profile_sha256",
                    "runtime_attestation_sha256",
                    "container_id",
                    "image",
                    "scenario_id",
                    "scenario_manifest_sha256",
                    "world_source",
                    "world_reference",
                    "reference_point",
                    "verified_home_enu_m",
                    "home_position",
                    "clock_id",
                    "observed_at_s",
                    "qualified_at_s",
                    "expires_at_s",
                    "horizontal_uncertainty_m",
                    "vertical_uncertainty_m",
                    "source_evidence",
                    "verification",
                }
            ),
        )
        require_schema_version(value["schema_version"])
        if (
            value["contract"] != ORIGIN_CONTRACT
            or value["runtime_kind"] != "SIM"
            or value["vehicle_class"] != "multirotor"
        ):
            raise AutonomyContractError("origin metadata supports exact multirotor SIM scope only")
        for name in (
            "observed_context_sha256",
            "airframe_profile_sha256",
            "runtime_attestation_sha256",
            "scenario_manifest_sha256",
            "container_id",
        ):
            require_sha256(value[name], name)
        for name in ("airframe_profile_id", "image", "scenario_id", "clock_id"):
            require_text(value[name], name)
        if value["reference_point"] != "PX4_navigation_reference_point":
            raise AutonomyContractError(
                "Gazebo model origin cannot substitute the PX4 navigation reference"
            )
        vector = require_array(value["verified_home_enu_m"], "verified home ENU")
        if len(vector) != 3:
            raise AutonomyContractError("verified home ENU must have three metre components")
        for entry in vector:
            require_real(entry, "verified home ENU component", minimum=-1000, maximum=1000)
        reference = strict_object(
            value["world_reference"],
            name="world reference",
            required=frozenset(
                {
                    "latitude_deg",
                    "longitude_deg",
                    "elevation_m",
                    "altitude_datum",
                    "world_frame",
                    "heading_rad",
                }
            ),
        )
        require_real(reference["heading_rad"], "heading_rad")
        if (
            reference["world_frame"] != "ENU"
            or reference["altitude_datum"] != "SDF_world_elevation"
            or reference["heading_rad"] != 0
        ):
            raise AutonomyContractError("origin reference frame/datum/heading is unsupported")
        for name in ("latitude_deg", "longitude_deg", "elevation_m"):
            require_real(reference[name], name)
        for name in ("observed_at_s", "qualified_at_s", "expires_at_s"):
            require_real(value[name], name, minimum=0)
        if (
            not value["observed_at_s"]
            <= value["qualified_at_s"]
            < value["expires_at_s"]
            <= value["observed_at_s"] + MAXIMUM_AGE_S
        ):
            raise AutonomyContractError("origin times are unordered or renew an old observation")
        for name in ("horizontal_uncertainty_m", "vertical_uncertainty_m"):
            require_real(value[name], name, minimum=0.0001, maximum=2.0)
        verification = strict_object(
            value["verification"], name="origin verification", required=_ORIGIN_VERIFICATION_FIELDS
        )
        if any(flag is not True for flag in verification.values()):
            raise AutonomyContractError(
                "origin pose/extrinsic/reset qualification remains incomplete"
            )
        EvidenceBinding.from_mapping(value["world_source"])
        sources = require_array(value["source_evidence"], "origin source evidence")
        if not 3 <= len(sources) <= 64:
            raise AutonomyContractError(
                "origin needs bounded home/pose/extrinsic/reset source evidence"
            )
        for source in sources:
            EvidenceBinding.from_mapping(source)
        return cls(canonical_json(value))

    def to_mapping(self) -> dict[str, object]:
        return strict_json_loads(self._document_json)

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @property
    def home_enu_m(self) -> tuple[float, float, float]:
        return tuple(self.to_mapping()["verified_home_enu_m"])


@dataclass(frozen=True, slots=True)
class ExplicitSimAirframeEnvelope:
    """Bound engineering envelope about the actual navigation reference.

    Physical offsets must cover the vehicle's complete reviewed yaw/tilt range.
    A checked model-source SHA is identity, not a proof that small bounds are
    conservative. The producer must review/qualify those bounds separately.
    """

    _document_json: str

    @classmethod
    def from_mapping(cls, raw: object) -> ExplicitSimAirframeEnvelope:
        value = strict_object(
            raw,
            name="explicit SIM clearance envelope",
            required=frozenset(
                {
                    "contract",
                    "schema_version",
                    "runtime_kind",
                    "vehicle_class",
                    "image",
                    "airframe_profile_id",
                    "airframe_profile_sha256",
                    "reference_point",
                    "horizontal_radius_m",
                    "bottom_offset_m",
                    "top_offset_m",
                    "horizontal_tracking_margin_m",
                    "vertical_tracking_margin_m",
                    "gps_horizontal_accuracy_multiplier",
                    "projection_margin_m",
                    "contact_tolerance_m",
                    "contact_collision_id",
                    "model_sources",
                    "provenance",
                    "physical_calibration_claimed",
                }
            ),
        )
        require_schema_version(value["schema_version"])
        if (
            value["contract"] != ENVELOPE_CONTRACT
            or value["runtime_kind"] != "SIM"
            or value["vehicle_class"] != "multirotor"
        ):
            raise AutonomyContractError("clearance envelope supports multirotor SIM only")
        if value["reference_point"] != "PX4_navigation_reference_point":
            raise AutonomyContractError("airframe envelope reference point is not qualified")
        require_sha256(value["airframe_profile_sha256"], "airframe profile SHA")
        for name in ("image", "airframe_profile_id", "provenance", "contact_collision_id"):
            require_text(value[name], name)
        require_real(
            value["horizontal_radius_m"], "airframe horizontal radius", minimum=0.1, maximum=5.0
        )
        bottom = require_real(
            value["bottom_offset_m"], "airframe bottom offset", minimum=-5, maximum=0
        )
        top = require_real(value["top_offset_m"], "airframe top offset", minimum=0, maximum=5)
        if top <= bottom:
            raise AutonomyContractError("airframe vertical envelope must have positive thickness")
        require_real(
            value["horizontal_tracking_margin_m"],
            "horizontal tracking margin",
            minimum=0.0,
            maximum=5,
        )
        require_real(
            value["vertical_tracking_margin_m"], "vertical tracking margin", minimum=0.01, maximum=5
        )
        require_real(
            value["gps_horizontal_accuracy_multiplier"],
            "GPS margin multiplier",
            minimum=1,
            maximum=10,
        )
        require_real(value["projection_margin_m"], "projection margin", minimum=0.05, maximum=2)
        require_real(value["contact_tolerance_m"], "contact tolerance", minimum=0.001, maximum=0.03)
        if require_bool(value["physical_calibration_claimed"], "physical calibration claimed"):
            raise AutonomyContractError(
                "explicit SIM envelope is not physical calibration evidence"
            )
        sources = require_array(value["model_sources"], "airframe model sources")
        if not 1 <= len(sources) <= 16:
            raise AutonomyContractError("airframe envelope requires bounded model source evidence")
        for source in sources:
            EvidenceBinding.from_mapping(source)
        return cls(canonical_json(value))

    def to_mapping(self) -> dict[str, object]:
        return strict_json_loads(self._document_json)

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


@dataclass(frozen=True, slots=True)
class StaticTeacherRouteClearance:
    observed_context_sha256: str
    geometry_sha256: str
    legs_sha256: str
    runtime_attestation_sha256: str
    world_source: EvidenceBinding
    origin_source: EvidenceBinding
    airframe_envelope_source: EvidenceBinding
    _details_json: str

    @property
    def route_collision_clear(self) -> bool:
        return self.to_mapping()["route_collision_clear"]

    @property
    def collision_hits(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (hit["phase"], hit["collision_id"]) for hit in self.to_mapping()["collision_hits"]
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "contract": ROUTE_CONTRACT,
            "schema_version": 1,
            "observed_context_sha256": self.observed_context_sha256,
            "geometry_sha256": self.geometry_sha256,
            "legs_sha256": self.legs_sha256,
            "runtime_attestation_sha256": self.runtime_attestation_sha256,
            "world_source": self.world_source.to_mapping(),
            "origin_source": self.origin_source.to_mapping(),
            "airframe_envelope_source": self.airframe_envelope_source.to_mapping(),
            **strict_json_loads(self._details_json),
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


def _local_geodetic_offset_enu(point, home, reference):
    """WGS-84 ECEF differences rotated onto the SDF origin's ENU basis.

    Both geodetic points use the same SDF reference elevation; relative-home
    navigation altitude is applied separately to the verified home UP value.
    No GPS MSL/ellipsoid altitude or world-zero guess enters this conversion.
    """

    def ecef(latitude, longitude):
        lat, lon = math.radians(latitude), math.radians(longitude)
        e2 = WGS84_FLATTENING * (2.0 - WGS84_FLATTENING)
        n = WGS84_SEMI_MAJOR_AXIS_M / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        elevation = reference["elevation_m"]
        return (
            (n + elevation) * math.cos(lat) * math.cos(lon),
            (n + elevation) * math.cos(lat) * math.sin(lon),
            (n * (1 - e2) + elevation) * math.sin(lat),
        )

    first, second = (
        ecef(home.latitude_deg, home.longitude_deg),
        ecef(point.latitude_deg, point.longitude_deg),
    )
    delta = tuple(second[i] - first[i] for i in range(3))
    lat, lon = math.radians(reference["latitude_deg"]), math.radians(reference["longitude_deg"])
    east = -math.sin(lon) * delta[0] + math.cos(lon) * delta[1]
    north = (
        -math.sin(lat) * math.cos(lon) * delta[0]
        - math.sin(lat) * math.sin(lon) * delta[1]
        + math.cos(lat) * delta[2]
    )
    if math.hypot(east, north) > 1000:
        raise AutonomyContractError("teacher route exceeds the supported local projection domain")
    return east, north


def _segment_hits_box(start, end, minimum, maximum):
    lower, upper = 0.0, 1.0
    for index in range(3):
        distance = end[index] - start[index]
        if abs(distance) < 1e-12:
            if start[index] < minimum[index] or start[index] > maximum[index]:
                return False
        else:
            first, second = (
                (minimum[index] - start[index]) / distance,
                (maximum[index] - start[index]) / distance,
            )
            lower, upper = max(lower, min(first, second)), min(upper, max(first, second))
            if lower > upper:
                return False
    return True


def _support_contains_footprint(collision, home, radius):
    if collision.kind == "plane":
        return True
    if collision.kind not in {"box", "cylinder"}:
        return False
    # Only horizontal top surfaces are allowed as the ground contact support.
    if max(abs(collision.rotation[i][2] - (0, 0, 1)[i]) for i in range(3)) > 1e-10:
        return False
    for east, north in itertools.product((-radius, radius), repeat=2):
        delta = (
            home[0] + east - collision.center_enu_m[0],
            home[1] + north - collision.center_enu_m[1],
            0,
        )
        local = tuple(sum(collision.rotation[j][i] * delta[j] for j in range(3)) for i in range(3))
        if collision.kind == "box" and any(
            abs(local[i]) > collision.half_extents_local_m[i] for i in (0, 1)
        ):
            return False
        if (
            collision.kind == "cylinder"
            and math.hypot(local[0], local[1]) > collision.half_extents_local_m[0]
        ):
            return False
    return True


def evaluate_static_teacher_route(
    *,
    artifact_root: str | Path,
    world_source: EvidenceBinding,
    include_model_sources: Mapping[str, EvidenceBinding],
    origin_source: EvidenceBinding,
    airframe_envelope_source: EvidenceBinding,
    observed_context: ContextSnapshot,
    geometry: MultirotorMissionGeometry,
    legs: tuple[HeldTeacherLeg, ...],
    runtime_attestation: ExternalRuntimeAttestation,
    now_s: float,
    clock_id: str,
) -> StaticTeacherRouteClearance:
    """Audit full source-bound swept geometry without altering observation flags.

    A positive result describes this static route/envelope only. It grants no
    Mission, live hazard, flight, operator or actuator permission. The caller
    must establish origin semantics and actual loaded asset identity first.
    """
    from ..px4_mission import MultirotorMissionGeometry
    from .contracts import ContextSnapshot, GeoPoint
    from .onboard_teacher_mission import HeldTeacherLeg
    from .sim_planning_inputs import _checked_attestation

    if (
        type(observed_context) is not ContextSnapshot
        or observed_context.schema_version != 2
        or observed_context.provenance.runtime_kind != "SIM"
    ):
        raise AutonomyContractError("static route audit requires original ContextSnapshot v2 SIM")
    if type(geometry) is not MultirotorMissionGeometry:
        raise TypeError("geometry must be MultirotorMissionGeometry")
    if (
        type(legs) is not tuple
        or not 2 <= len(legs) <= 16
        or any(type(leg) is not HeldTeacherLeg for leg in legs)
    ):
        raise AutonomyContractError("teacher route requires 2..16 typed held legs")
    if len(legs) != len(geometry.waypoints) or tuple(leg.sequence for leg in legs) != tuple(
        range(1, len(legs) + 1)
    ):
        raise AutonomyContractError("teacher held legs do not match route waypoints")
    now = require_real(now_s, "now_s", minimum=0)
    clock = require_text(clock_id, "clock_id")
    if not observed_context.captured_at_s <= now < observed_context.captured_at_s + MAXIMUM_AGE_S:
        raise AutonomyContractError("original route observation is future or stale")
    attestation = _checked_attestation(runtime_attestation)
    if attestation.vehicle_class != "multirotor":
        raise AutonomyContractError("static teacher route requires multirotor SIM attestation")
    root, identities = Path(artifact_root).resolve(), {}
    origin = ExplicitSimOriginBinding.from_mapping(_bound_json(root, origin_source, identities))
    od = origin.to_mapping()
    envelope = ExplicitSimAirframeEnvelope.from_mapping(
        _bound_json(root, airframe_envelope_source, identities)
    )
    ed = envelope.to_mapping()
    if (
        od["clock_id"] != clock
        or not od["qualified_at_s"] <= now < od["expires_at_s"]
        or od["observed_at_s"] < observed_context.captured_at_s
    ):
        raise AutonomyContractError("origin clock/window is not current for original observations")
    if (
        od["observed_context_sha256"] != observed_context.canonical_hash
        or GeoPoint.from_mapping(od["home_position"]) != observed_context.home_position
    ):
        raise AutonomyContractError("origin does not bind the original observed context/home")
    if od["runtime_attestation_sha256"] != attestation.canonical_hash:
        raise AutonomyContractError("origin runtime attestation SHA differs")
    for key in (
        "container_id",
        "image",
        "scenario_id",
        "scenario_manifest_sha256",
        "airframe_profile_id",
    ):
        if od[key] != getattr(attestation, key):
            raise AutonomyContractError(f"origin runtime {key} differs")
    if (
        od["airframe_profile_id"] != observed_context.airframe_profile_id
        or od["airframe_profile_sha256"] != observed_context.airframe_profile_hash
        or ed["airframe_profile_id"] != observed_context.airframe_profile_id
        or ed["airframe_profile_sha256"] != observed_context.airframe_profile_hash
        or ed["image"] != attestation.image
        or EvidenceBinding.from_mapping(od["world_source"]) != world_source
    ):
        raise AutonomyContractError("origin/envelope/world/airframe exact scope differs")
    upstream_sources = tuple(
        EvidenceBinding.from_mapping(v) for v in od["source_evidence"] + ed["model_sources"]
    )
    for source in upstream_sources:
        _bound_bytes(root, source, MAXIMUM_INPUT_BYTES, identities)
    scene = inspect_static_gazebo_scene(
        artifact_root=root, world_source=world_source, include_model_sources=include_model_sources
    )
    reference = od["world_reference"]
    if (reference["latitude_deg"], reference["longitude_deg"], reference["elevation_m"]) != (
        scene.reference_latitude_deg,
        scene.reference_longitude_deg,
        scene.reference_elevation_m,
    ):
        raise AutonomyContractError("origin reference differs from loaded static world bytes")
    home = observed_context.home_position
    if (geometry.home.latitude_deg, geometry.home.longitude_deg) != (
        home.latitude_deg,
        home.longitude_deg,
    ):
        raise AutonomyContractError("route home differs from verified observed home")
    final = geometry.waypoints[-1]
    if (final.latitude_deg, final.longitude_deg) != (
        home.latitude_deg,
        home.longitude_deg,
    ) or final.altitude_m <= 0:
        raise AutonomyContractError("explicit elevated home-return waypoint required before LAND")
    if (
        geometry.landing.latitude_deg,
        geometry.landing.longitude_deg,
        geometry.landing.altitude_m,
    ) != (home.latitude_deg, home.longitude_deg, 0):
        raise AutonomyContractError("only an explicit vertical return-home landing is supported")
    verified_home = origin.home_enu_m
    if (
        observed_context.aircraft_position.altitude_reference != "relative_home"
        or observed_context.aircraft_position.altitude_m is None
    ):
        raise AutonomyContractError(
            "observed aircraft requires source-bound relative-home altitude"
        )
    # Keep the nominal observed GPS/home offset in the initial point. Its GPS
    # uncertainty remains an independent tube, rather than being consumed
    # once to accept the offset and then erase that offset from the route.
    aircraft_offset = _local_geodetic_offset_enu(
        observed_context.aircraft_position, home, reference
    )
    gps_margin = (
        observed_context.gps_horizontal_accuracy_m * ed["gps_horizontal_accuracy_multiplier"]
    )
    if math.hypot(*aircraft_offset) > gps_margin + od["horizontal_uncertainty_m"]:
        raise AutonomyContractError("observed aircraft/home offset exceeds origin/GPS envelope")
    horizontal = (
        ed["horizontal_radius_m"]
        + gps_margin
        + ed["horizontal_tracking_margin_m"]
        + ed["projection_margin_m"]
        + od["horizontal_uncertainty_m"]
        + max(geometry.acceptance_radius_m, geometry.loiter_radius_m)
    )
    vertical_margin = (
        ed["vertical_tracking_margin_m"]
        + od["vertical_uncertainty_m"]
        + geometry.acceptance_radius_m
    )
    physical_bottom = verified_home[2] + ed["bottom_offset_m"]
    start_home_up = verified_home[2] + observed_context.aircraft_position.altitude_m
    start_physical_bottom = start_home_up + ed["bottom_offset_m"]
    verified_start = (
        verified_home[0] + aircraft_offset[0],
        verified_home[1] + aircraft_offset[1],
        start_home_up,
    )
    support = next(
        (
            collision
            for collision in scene.collisions
            if collision.collision_id == ed["contact_collision_id"]
        ),
        None,
    )
    if support is None or not all(
        _support_contains_footprint(support, point, horizontal)
        for point in (verified_start, verified_home)
    ):
        raise AutonomyContractError(
            "declared contact support does not contain conservative home footprint"
        )
    if (
        physical_bottom < support.upper_surface_up_m - 1e-9
        or physical_bottom - support.upper_surface_up_m > ed["contact_tolerance_m"]
    ):
        raise AutonomyContractError(
            "ground contact reference penetrates or is detached from verified support"
        )
    if (
        start_physical_bottom < support.upper_surface_up_m - 1e-9
        or start_physical_bottom - support.upper_surface_up_m > ed["contact_tolerance_m"]
    ):
        raise AutonomyContractError(
            "observed takeoff reference penetrates or is detached from support"
        )
    points = [
        ("ground_start", verified_start),
        (
            "takeoff",
            (verified_home[0], verified_home[1], verified_home[2] + geometry.takeoff_altitude_m),
        ),
    ]
    for index, point in enumerate(geometry.waypoints):
        east, north = _local_geodetic_offset_enu(point, home, reference)
        points.append(
            (
                f"waypoint-{index + 1}",
                (
                    verified_home[0] + east,
                    verified_home[1] + north,
                    verified_home[2] + point.altitude_m,
                ),
            )
        )
    points.append(("ground_landing", tuple(verified_home)))
    hits, contact_exceptions, audited = [], [], []
    intervals = [("takeoff", points[0][1], points[1][1])]
    intervals += [
        (f"transit-{i + 1}", points[i + 1][1], points[i + 2][1]) for i in range(len(legs))
    ]
    intervals += [(f"hold-{i + 1}", points[i + 2][1], points[i + 2][1]) for i in range(len(legs))]
    intervals += [
        ("terminal_loiter", points[-2][1], points[-2][1]),
        ("landing", points[-2][1], points[-1][1]),
    ]
    for phase, start, end in intervals:
        audited.append({"phase": phase, "start_enu_m": list(start), "end_enu_m": list(end)})
        for collision in scene.collisions:
            contact_phase = phase in {"takeoff", "landing"}
            # Only the declared contact support or a horizontal substrate
            # beneath it that contains the complete contact footprint may
            # tolerate the uncertainty tube at ground contact. A detached low
            # platform is not excused merely because its world Z is low.
            contact_points = (
                (verified_start, verified_home) if phase == "takeoff" else (verified_home,)
            )
            below_contact_support = (
                collision.upper_surface_up_m <= support.upper_surface_up_m + 1e-9
                and all(
                    _support_contains_footprint(collision, point, horizontal)
                    for point in contact_points
                )
            )
            if contact_phase and below_contact_support:
                contact_exceptions.append(
                    {
                        "phase": phase,
                        "collision_id": collision.collision_id,
                        "basis": "contains_contact_footprint_at_or_below_verified_support",
                    }
                )
                continue
            if collision.kind == "plane":
                hit = (
                    min(start[2], end[2]) + ed["bottom_offset_m"] - vertical_margin
                    <= collision.upper_surface_up_m
                )
            else:
                minimum = (
                    collision.minimum_enu_m[0] - horizontal,
                    collision.minimum_enu_m[1] - horizontal,
                    collision.minimum_enu_m[2] - ed["top_offset_m"] - vertical_margin,
                )
                maximum = (
                    collision.maximum_enu_m[0] + horizontal,
                    collision.maximum_enu_m[1] + horizontal,
                    collision.maximum_enu_m[2] - ed["bottom_offset_m"] + vertical_margin,
                )
                hit = _segment_hits_box(start, end, minimum, maximum)
            if hit:
                hits.append({"phase": phase, "collision_id": collision.collision_id})
    _bound_bytes(root, origin_source, MAXIMUM_INPUT_BYTES, identities)
    _bound_bytes(root, airframe_envelope_source, MAXIMUM_INPUT_BYTES, identities)
    for source in upstream_sources:
        _bound_bytes(root, source, MAXIMUM_INPUT_BYTES, identities)
    # Reinspect immutable scene bytes after deriving all path intersections.
    if (
        inspect_static_gazebo_scene(
            artifact_root=root,
            world_source=world_source,
            include_model_sources=include_model_sources,
        )
        != scene
    ):
        raise AutonomyContractError("static scene changed during route clearance")
    details = {
        "runtime_kind": "SIM",
        "vehicle_class": "multirotor",
        "clock_id": clock,
        "evaluated_at_s": now,
        "expires_at_s": min(od["expires_at_s"], observed_context.captured_at_s + MAXIMUM_AGE_S),
        "scene_sha256": scene.canonical_hash,
        "origin_metadata_sha256": origin.canonical_hash,
        "airframe_envelope_sha256": envelope.canonical_hash,
        "include_sources": {uri: source.to_mapping() for uri, source in scene.include_sources},
        "route_collision_clear": not hits,
        "collision_hits": hits,
        "collision_count": len(scene.collisions),
        "trajectory": [{"point_id": tag, "position_enu_m": list(point)} for tag, point in points],
        "audited_intervals": audited,
        "horizontal_inflation_m": horizontal,
        "vertical_tracking_acceptance_uncertainty_m": vertical_margin,
        "physical_bottom_offset_m": ed["bottom_offset_m"],
        "physical_top_offset_m": ed["top_offset_m"],
        "contact_support_id": support.collision_id,
        "contact_phase_exceptions": contact_exceptions,
        "claims": {
            name: False
            for name in (
                "origin_packet_semantics_independently_verified_by_this_module",
                "airframe_envelope_physically_calibrated",
                "dynamic_obstacles_cleared",
                "runtime_assets_independently_reinspected",
                "scene_truth_sent_to_actor",
                "resource_flags_modified",
                "mission_approved",
                "mission_submitted",
                "mission_executed",
                "execution_authority",
                "hardware_control_enabled",
                "train_admitted",
                "final_release_allowed",
            )
        },
    }
    return StaticTeacherRouteClearance(
        observed_context.canonical_hash,
        canonical_sha256(geometry.to_mapping()),
        canonical_sha256({"legs": [leg.to_mapping() for leg in legs]}),
        attestation.canonical_hash,
        world_source,
        origin_source,
        airframe_envelope_source,
        canonical_json(details),
    )


def validate_static_teacher_route_clearance(
    receipt: StaticTeacherRouteClearance,
    *,
    observed_context: ContextSnapshot,
    geometry: MultirotorMissionGeometry,
    legs: tuple[HeldTeacherLeg, ...],
    runtime_attestation: ExternalRuntimeAttestation,
    artifact_root: str | Path,
    now_s: float,
    clock_id: str,
) -> None:
    """Recompute a development receipt from its exact bound inputs and sources.

    No stored PASS or caller-supplied dataclass is trusted without this full
    recomputation. Verification retains the original evaluation/expiry window;
    reading a receipt later never refreshes observations or origin evidence.
    Source identity is still not independent origin-packet semantics, physical
    calibration, live-asset attestation or permission to submit a Mission.
    """
    if type(receipt) is not StaticTeacherRouteClearance:
        raise TypeError("receipt must be StaticTeacherRouteClearance")
    document = receipt.to_mapping()
    now = require_real(now_s, "now_s", minimum=0)
    clock = require_text(clock_id, "clock_id")
    evaluated = require_real(document["evaluated_at_s"], "evaluated_at_s", minimum=0)
    expires = require_real(document["expires_at_s"], "expires_at_s", minimum=0)
    if document["clock_id"] != clock or not evaluated <= now < expires:
        raise AutonomyContractError("static route receipt is future/stale or from another clock")
    includes = document["include_sources"]
    if not isinstance(includes, Mapping):
        raise AutonomyContractError("static route include sources must be exact bindings")
    expected = evaluate_static_teacher_route(
        artifact_root=artifact_root,
        world_source=receipt.world_source,
        include_model_sources={
            uri: EvidenceBinding.from_mapping(source) for uri, source in includes.items()
        },
        origin_source=receipt.origin_source,
        airframe_envelope_source=receipt.airframe_envelope_source,
        observed_context=observed_context,
        geometry=geometry,
        legs=legs,
        runtime_attestation=runtime_attestation,
        now_s=evaluated,
        clock_id=clock,
    )
    if expected.to_mapping() != document:
        raise AutonomyContractError(
            "static route receipt differs from recomputed bound sources/context/path"
        )


__all__ = [
    "StaticCollision",
    "StaticGazeboScene",
    "ExplicitSimOriginBinding",
    "ExplicitSimAirframeEnvelope",
    "StaticTeacherRouteClearance",
    "inspect_static_gazebo_scene",
    "evaluate_static_teacher_route",
    "validate_static_teacher_route_clearance",
]
