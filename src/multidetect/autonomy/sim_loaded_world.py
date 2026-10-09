"""Compare retained Gazebo-generated static collision definitions with sources.

The read-only generate_world_sdf reply describes the server's loaded entities.
It does not prove solver contact, sensor timing, runtime identity, or permission
to fly. Dynamic vehicle geometry is excluded explicitly, not treated as empty
space for any other model. Original service text and declared files are reopened.
"""

from __future__ import annotations

import copy
import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .onboard_executed_trajectory import EvidenceBinding
from .serialization import (
    AutonomyContractError,
    canonical_json,
    canonical_sha256,
    strict_json_loads,
)
from .sim_static_route_clearance import (
    _IDENTITY,
    _ZERO,
    _model_collisions,
    _name,
    _one,
    _vector,
    inspect_static_gazebo_scene,
)
from .sim_world_inputs import _NoDtdBuilder, _read

CONTRACT = "multidetect.loaded-Gazebo-static-geometry-audit-v1"
_SPACE = " \t\r\n"
_STRING = re.compile(r'data\s*:\s*"((?:[^"\\\r\n]|\\.)*)"\s*', re.ASCII)
_TOLERANCE = 1e-8  # serialized metric definitions only, not navigation uncertainty


def decode_generated_sdf_reply(raw: bytes) -> bytes:
    """One bounded protobuf StringMsg text field, not Python/JSON evaluation."""
    if type(raw) is not bytes or not 0 < len(raw) <= 1024 * 1024:
        raise AutonomyContractError("bounded original StringMsg bytes required")
    try:
        text = raw.decode("ascii").strip(_SPACE)
    except UnicodeDecodeError as error:
        raise AutonomyContractError("StringMsg text must use ASCII protobuf escapes") from error
    match = _STRING.fullmatch(text)
    if match is None:
        raise AutonomyContractError("exact single data string required; no extra reply fields")
    value, output, index = match[1], bytearray(), 0
    escapes = {
        "a": 7,
        "b": 8,
        "f": 12,
        "n": 10,
        "r": 13,
        "t": 9,
        "v": 11,
        "\\": 92,
        "'": 39,
        '"': 34,
        "?": 63,
    }
    while index < len(value):
        char = value[index]
        index += 1
        if char != "\\":
            if ord(char) < 32:
                raise AutonomyContractError("unescaped StringMsg control character")
            output.append(ord(char))
            continue
        char = value[index]
        index += 1
        if char in escapes:
            output.append(escapes[char])
        elif char in "01234567":
            digits = char
            while index < len(value) and len(digits) < 3 and value[index] in "01234567":
                digits += value[index]
                index += 1
            number = int(digits, 8)
            if number > 255:
                raise AutonomyContractError("StringMsg octal byte overflow")
            output.append(number)
        elif char == "x":
            digits = value[index : index + 2]
            if len(digits) != 2 or re.fullmatch("[0-9a-fA-F]{2}", digits) is None:
                raise AutonomyContractError("exact two-digit StringMsg hex byte required")
            output.append(int(digits, 16))
            index += 2
        else:
            raise AutonomyContractError("unsupported StringMsg escape")
    if not output or 0 in output:
        raise AutonomyContractError("nonempty XML without embedded NUL required")
    return bytes(output)


def _xml(raw):
    try:
        root = ET.fromstring(raw, parser=ET.XMLParser(target=_NoDtdBuilder()))
    except (ET.ParseError, UnicodeError) as error:
        raise AutonomyContractError("malformed generated SDF") from error
    # These versions have the primitive/pose semantics used here. The server
    # may serialize a newer SDF than the original 1.9; no file is rewritten.
    if root.tag != "sdf" or root.attrib.get("version") not in {"1.9", "1.10", "1.11"}:
        raise AutonomyContractError("unsupported generated SDF version/root")
    if set(root.attrib) != {"version"} or len(root) != 1 or root[0].tag != "world":
        raise AutonomyContractError("one plain generated world required")
    return root


def _plain_pose(element):
    for pose in element.iter("pose"):
        # An explicitly empty relative_to is the default parent, not a named
        # frame. Never erase a nonempty frame, quaternion, or degrees flag.
        if pose.attrib not in ({}, {"relative_to": ""}):
            raise AutonomyContractError("named/nonstandard generated pose frame unsupported")
        pose.attrib.clear()


def _static_projection(model):
    """Keep collision-affecting content and only ignore explicit benign defaults."""
    result = copy.deepcopy(model)
    if any(x.tag in {"plugin", "actor", "frame", "include", "joint"} for x in result.iter()):
        raise AutonomyContractError("generated obstacle dynamic extensions unsupported")
    _plain_pose(result)
    harmless_model = {
        "self_collide": {"false", "0"},
        "allow_auto_disable": {"true", "1"},
        "enable_wind": {"false", "0"},
    }
    harmless_link = {"enable_wind": {"false", "0"}, "must_be_base_link": {"false", "0"}}
    for parent, harmless in [
        (result, harmless_model),
        *[(x, harmless_link) for x in result.findall("link")],
    ]:
        for child in list(parent):
            if child.tag in harmless:
                if (
                    child.attrib
                    or (child.text or "").strip(_SPACE) not in harmless[child.tag]
                    or len(child)
                ):
                    raise AutonomyContractError(
                        "nondefault generated model/link behavior unsupported"
                    )
                parent.remove(child)
    static = _one(result, "static")
    if static.attrib or len(static) or (static.text or "").strip(_SPACE) not in {"true", "1"}:
        raise AutonomyContractError("unexpected moving generated obstacle")
    static.text = "true"
    return result


def _reference(world):
    spherical = _one(world, "spherical_coordinates")
    if (_one(spherical, "surface_model").text or "").strip(_SPACE) != "EARTH_WGS84":
        raise AutonomyContractError("generated world datum differs")
    if (_one(spherical, "world_frame_orientation").text or "").strip(_SPACE) != "ENU":
        raise AutonomyContractError("generated world frame differs")
    heading = _one(spherical, "heading_deg", optional=True)
    if heading is not None and _vector(heading.text, 1, "heading")[0] != 0:
        raise AutonomyContractError("rotated generated world frame unsupported")
    return tuple(
        _vector(_one(spherical, key).text, 1, key)[0]
        for key in ("latitude_deg", "longitude_deg", "elevation")
    )


@dataclass(frozen=True, slots=True)
class LoadedStaticGeometryAudit:
    _document_json: str

    def to_mapping(self):
        return strict_json_loads(self._document_json)

    @property
    def canonical_hash(self):
        return canonical_sha256(self.to_mapping())

    @property
    def collision_definitions_match(self):
        return self.to_mapping()["collision_definitions_match"]


def audit_loaded_static_geometry(
    *,
    artifact_root: str | Path,
    generated_sdf_source: EvidenceBinding,
    world_source: EvidenceBinding,
    include_model_sources: Mapping[str, EvidenceBinding],
    vehicle_model_name: str = "x500_mono_cam_0",
) -> LoadedStaticGeometryAudit:
    """Independently compare declared primitives to a bound server SDF reply.

    The caller must establish native service completion/exact container identity
    and freshness independently. This pure-file audit has no live/flight grant.
    """
    if vehicle_model_name != "x500_mono_cam_0":
        raise AutonomyContractError("only the explicit x500 teacher vehicle exclusion supported")
    root = Path(artifact_root).resolve(strict=True)
    original = {"world": _read(root, world_source), "reply": _read(root, generated_sdf_source)}
    expected = inspect_static_gazebo_scene(
        artifact_root=root, world_source=world_source, include_model_sources=include_model_sources
    )
    declared_world = _xml(original["world"])[0]
    expected_names = {_name(m) for m in declared_world.findall("model")}
    for inc in declared_world.findall("include"):
        uri = (_one(inc, "uri").text or "").strip(_SPACE)
        name = _one(inc, "name", optional=True)
        expected_names.add(
            uri.removeprefix("model://") if name is None else name.text.strip(_SPACE)
        )
    generated_world = _xml(decode_generated_sdf_reply(original["reply"]))[0]
    if generated_world.attrib != {"name": expected.world_name}:
        raise AutonomyContractError("generated world name differs")
    if any(x.tag in {"include", "actor", "frame"} for x in generated_world):
        raise AutonomyContractError("generated obstacle includes/actors/frames must be resolved")
    models = generated_world.findall("model")
    names = [_name(m) for m in models]
    if len(set(names)) != len(names) or set(names) != expected_names | {vehicle_model_name}:
        raise AutonomyContractError("missing, extra or duplicated generated model")
    collisions = []
    for model in models:
        if _name(model) == vehicle_model_name:
            # This exclusion is recorded, not a claim about its airframe envelope.
            continue
        projected = _static_projection(model)
        collisions.extend(
            _model_collisions(
                projected, (_IDENTITY, _ZERO), _name(model), generated_sdf_source.sha256
            )
        )
    actual = {c.collision_id: c for c in collisions}
    desired = {c.collision_id: c for c in expected.collisions}
    if len(actual) != len(collisions):
        raise AutonomyContractError("duplicate generated collision identity")
    differences = []
    for name in sorted(set(actual) | set(desired)):
        if name not in actual or name not in desired:
            differences.append({"collision_id": name, "reason": "missing_or_extra_collision"})
            continue
        a, b = actual[name], desired[name]
        if a.kind != b.kind:
            differences.append({"collision_id": name, "reason": "primitive_kind_differs"})
            continue
        av = (*a.center_enu_m, *a.half_extents_local_m, *(x for row in a.rotation for x in row))
        bv = (*b.center_enu_m, *b.half_extents_local_m, *(x for row in b.rotation for x in row))
        if not all(
            math.isclose(x, y, rel_tol=0, abs_tol=_TOLERANCE) for x, y in zip(av, bv, strict=True)
        ):
            differences.append(
                {"collision_id": name, "reason": "metric_geometry_or_transform_differs"}
            )
    ref = _reference(generated_world)
    reference_match = all(
        math.isclose(x, y, rel_tol=0, abs_tol=_TOLERANCE)
        for x, y in zip(
            ref,
            (
                expected.reference_latitude_deg,
                expected.reference_longitude_deg,
                expected.reference_elevation_m,
            ),
            strict=True,
        )
    )
    if not reference_match:
        differences.append({"reason": "world_geographic_reference_differs"})
    # Reopen every input after computation. No source timestamps are renewed.
    if (
        _read(root, world_source) != original["world"]
        or _read(root, generated_sdf_source) != original["reply"]
    ):
        raise AutonomyContractError("loaded world inputs changed during audit")
    if (
        inspect_static_gazebo_scene(
            artifact_root=root,
            world_source=world_source,
            include_model_sources=include_model_sources,
        )
        != expected
    ):
        raise AutonomyContractError("declared collision inventory changed during audit")
    return LoadedStaticGeometryAudit(
        canonical_json(
            {
                "contract": CONTRACT,
                "world_source": world_source.to_mapping(),
                "generated_sdf_source": generated_sdf_source.to_mapping(),
                "include_model_sources": {
                    k: v.to_mapping() for k, v in include_model_sources.items()
                },
                "expected_scene_sha256": expected.canonical_hash,
                "expected_collision_count": len(desired),
                "generated_collision_count": len(actual),
                "collision_definitions_match": not differences,
                "world_reference_matches": reference_match,
                "differences": differences,
                "excluded_dynamic_vehicle": vehicle_model_name,
                "metric_comparison_tolerance_m": _TOLERANCE,
                "claims": {
                    k: False
                    for k in (
                        "live_identity_or_freshness_verified",
                        "physics_contact_verified",
                        "vehicle_envelope_qualified",
                        "origin_qualified",
                        "route_clearance_passed",
                        "execution_authority",
                        "physical_performance_claimed",
                        "final_release_allowed",
                    )
                },
            }
        )
    )
