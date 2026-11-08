"""Source-recomputed x500 collision envelope; not contact or flight admission.

The envelope encloses all four spinning collision boxes, every yaw, and an
explicit body-up tilt domain. Visual meshes and contact solver behavior are
not physical calibration. A smaller caller-supplied tilt domain is a design
assumption, not proof that PX4 enforces it. Ground contact remains unknown.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from pathlib import Path

from .onboard_executed_trajectory import EvidenceBinding
from .serialization import AutonomyContractError, canonical_json, canonical_sha256, require_real
from .sim_gimbal_collision import load_gimbal_collision_geometry
from .sim_loaded_world import _xml, decode_generated_sdf_reply
from .sim_navigation_reference import _x500_model_offset
from .sim_static_route_clearance import (
    _IDENTITY,
    _ZERO,
    _bound_bytes,
    _bound_json,
    _collision,
    _compose,
    _mv,
    _name,
    _one,
    _pose,
    _read_bound_sdf,
    _vector,
)

CONTRACT = "multidetect.source-derived-x500-collision-envelope-v1"
PINNED_IMAGE = (
    "px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8"
)
TOLERANCE = 1e-8  # loaded SDF serialization comparison, not a navigation margin
OUTWARD_PAD_M = 1e-7  # encloses comparison and floating-point arithmetic error
MAXIMUM_SOURCE_BYTES = 1024 * 1024
_LINKS = {"base_link", *(f"rotor_{i}" for i in range(4))}
_LOADED_LINKS = _LINKS | {"camera_link", "depth_camera_link"}
_ASSETS = {
    "x500": ("x500-model.sdf", "/opt/px4-gazebo/share/gz/models/x500/model.sdf"),
    "x500_base": (
        "x500-base-model.sdf",
        "/opt/px4-gazebo/share/gz/models/x500_base/model.sdf",
    ),
}


def _require(condition, message):
    if not condition:
        raise AutonomyContractError(message)


def _close(a, b):
    return len(a) == len(b) and all(
        math.isclose(x, y, abs_tol=TOLERANCE, rel_tol=0) for x, y in zip(a, b, strict=True)
    )


def _flat(rotation):
    return tuple(value for row in rotation for value in row)


def _corners(collision, offset):
    for signs in itertools.product((-1, 1), repeat=3):
        local = tuple(signs[i] * collision.half_extents_local_m[i] for i in range(3))
        delta = _mv(collision.rotation, local)
        yield tuple(collision.center_enu_m[i] + delta[i] - offset[i] for i in range(3))


def _maximum_up(rho, z, tilt):
    # max(z cos(delta) + rho sin(delta)), delta in the entire [0, tilt].
    # This is an analytic support bound, not a discretely sampled orientation.
    angles = [0.0, tilt]
    stationary = math.atan2(rho, z)
    if stationary <= tilt:
        angles.append(stationary)
    return max(z * math.cos(a) + rho * math.sin(a) for a in angles)


def _bounds(samples, tilt):
    _require(bool(samples), "nonempty complete collision envelope required")
    radius = max(math.hypot(rho, z) for rho, z in samples)
    return {
        # A sphere radius is deliberately retained for horizontal inflation,
        # even at zero tilt. It covers all yaw/tilt without angular sampling.
        "horizontal_radius_m": radius + OUTWARD_PAD_M,
        "bottom_offset_m": -max(_maximum_up(rho, -z, tilt) for rho, z in samples) - OUTWARD_PAD_M,
        "top_offset_m": max(_maximum_up(rho, z, tilt) for rho, z in samples) + OUTWARD_PAD_M,
    }


def _models(root, sources, identities):
    offset = _x500_model_offset(root, sources, identities)
    models = {
        name: _read_bound_sdf(root, source, identities)[0] for name, source in sources.items()
    }
    base = models["x500_base"]
    _require(
        all(child.tag in {"pose", "static", "self_collide", "link", "joint"} for child in base),
        "base nested includes, frames, plugins or extensions are unsupported",
    )
    links = base.findall("link")
    _require(
        len(links) == len(_LINKS) and {_name(link) for link in links} == _LINKS,
        "complete base_link and four rotor links required",
    )
    joints = base.findall("joint")
    _require(len(joints) == 4, "exactly four rotor joints required")
    for i in range(4):
        matches = [j for j in joints if j.attrib.get("name") == f"rotor_{i}_joint"]
        _require(len(matches) == 1, "unique rotor joint required")
        j = matches[0]
        _require(
            j.attrib == {"name": f"rotor_{i}_joint", "type": "revolute"}
            and _one(j, "parent").text == "base_link"
            and _one(j, "child").text == f"rotor_{i}"
            and j.find("pose") is None
            and j.find("axis2") is None
            and _one(_one(j, "axis"), "xyz").attrib == {}
            and _vector(_one(_one(j, "axis"), "xyz").text, 3, "rotor axis") == (0, 0, 1),
            "rotor needs a source-bound fixed-center body-up revolute axis",
        )
    for name in ("x500", "x500_mono_cam"):
        model = models[name]
        expected_includes = (
            {"model://x500_base"} if name == "x500" else {"model://x500", "model://mono_cam"}
        )
        includes = model.findall("include")
        _require(
            len(includes) == len(expected_includes)
            and {_one(item, "uri").text for item in includes} == expected_includes,
            "unreviewed included attachment requires new source closure",
        )
        _require(
            not model.findall("collision")
            and not model.findall("model")
            and not model.findall("frame"),
            "hidden outer model collision/frame composition unsupported",
        )
        for link in model.findall("link"):
            _require(not link.findall("collision"), "additional attachment collision needs review")
    all_collisions = list(base.iter("collision"))
    direct = [c for link in links for c in link.findall("collision")]
    _require(
        len(all_collisions) == len(direct) == 9, "complete exactly-nine collision closure required"
    )
    collisions = {}
    samples = []
    points = []
    for link in links:
        name = _name(link)
        children = link.findall("collision")
        _require(len(children) == (5 if name == "base_link" else 1), "collision count differs")
        for child in children:
            key = f"{name}/{_name(child)}"
            _require(key not in collisions, "duplicated collision identity")
            collision = _collision(
                child,
                _compose(_pose(base), _pose(link)),
                key,
                sources["x500_base"].sha256,
            )
            _require(collision.kind == "box", "only reviewed x500 collision boxes supported")
            _require(
                all(0 < v <= 2 for v in collision.half_extents_local_m),
                "airframe box size outside bounded domain",
            )
            collisions[key] = collision
            if name == "base_link":
                for point in _corners(collision, offset):
                    points.append(point)
                    samples.append((math.hypot(*point[:2]), point[2]))
            else:
                _require(
                    _pose(child) == (_IDENTITY, _ZERO) and _pose(link)[0] == _IDENTITY,
                    "rotor collision/link custom orientation or off-axis placement unsupported",
                )
                center = tuple(collision.center_enu_m[i] - offset[i] for i in range(3))
                hx, hy, hz = collision.half_extents_local_m
                swept_radius = math.hypot(hx, hy)
                rho = math.hypot(*center[:2]) + swept_radius
                samples.extend((rho, center[2] + sign * hz) for sign in (-1, 1))
    return models, offset, collisions, samples, points


def _extraction(root, source, models, identities):
    receipt = _bound_json(root, source, identities)
    _require(
        receipt.get("contract") == "multidetect.teacher-Gazebo-navref-asset-extraction-v1"
        and receipt.get("status") == "original-image-assets-recorded"
        and receipt.get("image") == PINNED_IMAGE
        and receipt.get("injected_native_used") is False
        and receipt.get("never_started_identity_verified") is True
        and receipt.get("owned_container_absent_after_cleanup") is True
        and receipt.get("error") is None
        and receipt.get("cleanup_error") is None,
        "complete pinned-image original extraction receipt required",
    )
    for name, (asset_name, image_path) in _ASSETS.items():
        asset = receipt["assets"][asset_name]
        v = asset["source"]
        _require(
            asset["image_path"] == image_path
            and Path(v["path"]).resolve() == (root / models[name].path).resolve()
            and (v["bytes"], v["sha256"]) == (models[name].bytes, models[name].sha256),
            "model input differs from retained fixed-image asset extraction",
        )
    return receipt


def _loaded(root, source, collisions, offset, models, identities):
    payload = _bound_bytes(root, source, MAXIMUM_SOURCE_BYTES, identities)
    document = _xml(decode_generated_sdf_reply(payload))
    _require(len(document) == 1 and document[0].tag == "world", "single generated world required")
    world = document[0]
    matches = [m for m in world.findall("model") if m.attrib.get("name") == "x500_mono_cam_0"]
    _require(len(matches) == 1, "exactly one loaded teacher vehicle required")
    vehicle = matches[0]
    _require(
        not any(x.tag in {"include", "model"} for x in vehicle),
        "loaded vehicle has unresolved/nested composition",
    )
    camera_include = next(
        i
        for i in models["x500_mono_cam"].findall("include")
        if _one(i, "uri").text == "model://mono_cam"
    )
    camera_pose = _one(camera_include, "pose")
    _require(camera_pose.attrib == {"relative_to": "base_link"}, "camera frame unsupported")
    frame_definitions = {
        "_merged__x500__model__": ("base_link", "__model__", (0, 0, 0, 0, 0, 0)),
        "_merged__x500_base__model__": ("base_link", "_merged__x500__model__", (*offset, 0, 0, 0)),
        "_merged__mono_cam__model__": (
            "camera_link",
            "base_link",
            _vector(camera_pose.text, 6, "camera pose"),
        ),
    }
    frames = vehicle.findall("frame")
    _require(
        len(frames) == 3 and {_name(f) for f in frames} == set(frame_definitions),
        "exact source-derived expanded merge frames required",
    )
    for frame in frames:
        attached, relative, values = frame_definitions[_name(frame)]
        pose = _one(frame, "pose")
        _require(
            frame.attrib == {"name": _name(frame), "attached_to": attached}
            and len(frame) == 1
            and pose.attrib == {"relative_to": relative}
            and _close(_vector(pose.text, 6, "merged frame pose"), values),
            "expanded frame transform differs from original model merge",
        )
    links = vehicle.findall("link")
    _require(
        len(links) == 7 and {_name(link) for link in links} == _LOADED_LINKS,
        "loaded vehicle complete link closure differs",
    )
    actual = {}
    contact_declarations = {}
    for link in links:
        name = _name(link)
        for child in link.findall("collision"):
            key = f"{name}/{_name(child)}"
            _require(key not in actual, "duplicate loaded collision")
            actual[key] = _collision(child, _pose(link), key, source.sha256)
            contact = child.find("surface/contact/ode")
            contact_declarations[key] = {
                "min_depth_text": None if contact is None else contact.findtext("min_depth"),
                "max_vel_text": None if contact is None else contact.findtext("max_vel"),
                "collide_bitmask_text": child.findtext("surface/contact/collide_bitmask"),
            }
    _require(
        len(list(vehicle.iter("collision"))) == len(actual) == 9 and set(actual) == set(collisions),
        "generated vehicle collision list is missing/extra or hidden",
    )
    for key, expected in collisions.items():
        found = actual[key]
        _require(
            found.kind == expected.kind
            and _close(found.center_enu_m, expected.center_enu_m)
            and _close(found.half_extents_local_m, expected.half_extents_local_m),
            "loaded collision geometry/center differs from model originals",
        )
        if key.startswith("base_link/"):
            _require(
                _close(_flat(found.rotation), _flat(expected.rotation)),
                "loaded rigid body collision rotation differs",
            )
        else:
            _require(
                _close(tuple(found.rotation[i][2] for i in range(3)), (0, 0, 1)),
                "loaded rotor axis differs from source-derived sweep",
            )
    nav = _pose(next(link for link in links if _name(link) == "base_link"))
    _require(
        _close(nav[1], offset) and _close(_flat(nav[0]), _flat(_IDENTITY)),
        "loaded body frame differs",
    )
    return world, vehicle, actual, contact_declarations


def _loaded_gimbal(root, source, geometry, identities):
    payload = _bound_bytes(root, source, MAXIMUM_SOURCE_BYTES, identities)
    document = _xml(decode_generated_sdf_reply(payload))
    _require(len(document) == 1 and document[0].tag == "world", "single generated world required")
    world = document[0]
    matches = [m for m in world.findall("model") if m.attrib.get("name") == "x500_gimbal_0"]
    _require(len(matches) == 1, "exactly one loaded x500 gimbal vehicle required")
    vehicle = matches[0]
    _require(
        not any(x.tag in {"include", "model"} for x in vehicle),
        "loaded gimbal vehicle has unresolved/nested composition",
    )
    links = vehicle.findall("link")
    expected_links = _LINKS | {
        "cgo3_mount_link",
        "cgo3_vertical_arm_link",
        "cgo3_horizontal_arm_link",
        "camera_link",
    }
    _require(
        len(links) == len(expected_links) and {_name(link) for link in links} == expected_links,
        "loaded x500 gimbal link closure differs",
    )
    actual = {}
    declarations = {}
    for link in links:
        name = _name(link)
        for child in link.findall("collision"):
            key = f"{name}/{_name(child)}"
            _require(key not in actual, "duplicate loaded gimbal collision")
            actual[key] = _collision(child, _pose(link), key, source.sha256)
            contact = child.find("surface/contact/ode")
            declarations[key] = {
                "min_depth_text": None if contact is None else contact.findtext("min_depth"),
                "max_vel_text": None if contact is None else contact.findtext("max_vel"),
                "collide_bitmask_text": child.findtext("surface/contact/collide_bitmask"),
            }
    expected = geometry.collision_definitions
    _require(
        len(list(vehicle.iter("collision"))) == len(actual) == len(expected) == 10
        and set(actual) == set(expected),
        "generated gimbal collision closure is missing/extra or hidden",
    )
    for key, original in expected.items():
        found = actual[key]
        _require(
            found.kind == original.kind
            and _close(found.center_enu_m, original.center_enu_m)
            and _close(found.half_extents_local_m, original.half_extents_local_m),
            "loaded gimbal collision geometry/center differs from original SDF expansion",
        )
        if key.startswith("base_link/"):
            _require(
                _close(_flat(found.rotation), _flat(original.rotation)),
                "loaded base collision rotation differs",
            )
        elif key.startswith("rotor_"):
            _require(
                _close(tuple(found.rotation[i][2] for i in range(3)), (0, 0, 1)),
                "loaded rotor axis differs from source-derived sweep",
            )
    nav = _pose(next(link for link in links if _name(link) == "base_link"))
    _require(
        _close(nav[1], geometry.model_offset) and _close(_flat(nav[0]), _flat(_IDENTITY)),
        "loaded gimbal body frame differs from source x500 base",
    )
    return world, vehicle, actual, declarations


@dataclass(frozen=True, slots=True)
class SourceDerivedSimAirframeEnvelopeAudit:
    _document_json: str

    def to_mapping(self):
        from .serialization import strict_json_loads

        return strict_json_loads(self._document_json)

    @property
    def canonical_hash(self):
        return canonical_sha256(self.to_mapping())

    @property
    def envelope_bounds(self):
        """Detached conservative bounds; contact/pose admission is NOT included."""
        return self.to_mapping()["envelope_bounds"]


def audit_x500_collision_envelope(
    *,
    artifact_root: str | Path,
    model_sources: dict[str, EvidenceBinding] | None = None,
    extraction_source: EvidenceBinding | None = None,
    generated_sdf_source: EvidenceBinding,
    gimbal_sources: dict[str, EvidenceBinding] | None = None,
    maximum_tilt_deg: float = 180.0,
) -> SourceDerivedSimAirframeEnvelopeAudit:
    """Recompute bounded source geometry; reject unknown shapes/frame semantics.

    Default 180 degrees covers all body orientations. Reduced tilt is an
    explicit design domain only, not an admitted/observed runtime capability.
    Geometry is provided for full 3D swept-route checks; there is deliberately
    no conversion into a contact-qualified legacy clearance envelope.
    """
    tilt = require_real(maximum_tilt_deg, "maximum_tilt_deg", minimum=0, maximum=180)
    root = Path(artifact_root).resolve(strict=True)
    identities = {}
    _require(
        type(generated_sdf_source) is EvidenceBinding and generated_sdf_source.json_pointer is None,
        "original generated SDF response required",
    )
    gimbal_geometry = None
    if gimbal_sources is None:
        _require(
            type(model_sources) is dict
            and set(model_sources) == {"x500_mono_cam", "x500", "x500_base"}
            and all(
                type(v) is EvidenceBinding and v.json_pointer is None
                for v in model_sources.values()
            ),
            "exact whole-file three-model source bindings required",
        )
        _require(
            type(extraction_source) is EvidenceBinding and extraction_source.json_pointer == "",
            "explicit whole extraction JSON required",
        )
        models, offset, collisions, samples, points = _models(root, model_sources, identities)
        _extraction(root, extraction_source, model_sources, identities)
        world, vehicle, actual, declarations = _loaded(
            root, generated_sdf_source, collisions, offset, models, identities
        )
    else:
        _require(
            model_sources is None and extraction_source is None,
            "fixed x500 and gimbal source closures cannot be mixed",
        )
        gimbal_geometry = load_gimbal_collision_geometry(root, gimbal_sources, identities)
        models = None
        offset = gimbal_geometry.model_offset
        collisions = gimbal_geometry.base_collisions
        samples = []
        points = []
        for key, collision in collisions.items():
            if key.startswith("base_link/"):
                for point in _corners(collision, offset):
                    points.append(point)
                    samples.append((math.hypot(*point[:2]), point[2]))
            else:
                center = tuple(collision.center_enu_m[i] - offset[i] for i in range(3))
                hx, hy, hz = collision.half_extents_local_m
                rho = math.hypot(*center[:2]) + math.hypot(hx, hy)
                samples.extend((rho, center[2] + sign * hz) for sign in (-1, 1))
        world, vehicle, actual, declarations = _loaded_gimbal(
            root, generated_sdf_source, gimbal_geometry, identities
        )
    pose = _pose(vehicle)
    loaded_body = _compose(pose, (_IDENTITY, offset))[1]
    world_points = []
    for key, collision in actual.items():
        if key.startswith("base_link/"):
            for point in _corners(collision, _ZERO):
                world_points.append(_compose(pose, (_IDENTITY, point))[1])
    pads = [m for m in world.findall("model") if m.attrib.get("name") == "multirotor_home_pad"]
    _require(len(pads) == 1, "retained home pad collision definition required")
    pad = pads[0]
    link = _one(pad, "link")
    pad_collision = _one(link, "collision")
    _require(
        _name(link) == "pad_link"
        and _name(pad_collision) == "pad_collision"
        and len(list(pad.iter("collision"))) == 1,
        "unique pad geometry required for overlap diagnostic",
    )
    support = _collision(
        pad_collision,
        _compose(_pose(pad), _pose(link)),
        "multirotor_home_pad/pad_link/pad_collision",
        generated_sdf_source.sha256,
    )
    _require(
        support.kind == "cylinder" and _close(_flat(support.rotation), _flat(_IDENTITY)),
        "level cylinder pad definition required; no contact assumption",
    )
    pad_top = support.center_enu_m[2] + support.half_extents_local_m[2]
    body_bottom = min(p[2] for p in world_points)
    gimbal_loaded_bottom = None
    if gimbal_geometry is not None:
        loaded_camera = actual["camera_link/cgo3_camera_collision"]
        camera_world = _compose(pose, (_IDENTITY, loaded_camera.center_enu_m))[1]
        gimbal_loaded_bottom = camera_world[2] - gimbal_geometry.sphere_radius_m
    envelope_bounds = _bounds(samples, math.radians(tilt))
    fixed_collision_bounds = dict(envelope_bounds)
    level_bounds = {
        "horizontal_radius_m": max(rho for rho, _ in samples) + OUTWARD_PAD_M,
        "bottom_offset_m": min(z for _, z in samples) - OUTWARD_PAD_M,
        "top_offset_m": max(z for _, z in samples) + OUTWARD_PAD_M,
    }
    gimbal_bounds = None
    gimbal_extension = None
    if gimbal_geometry is not None:
        gimbal_bounds = gimbal_geometry.swept_bounds(tilt)
        gimbal_extension = {
            "horizontal_radius_extension_m": max(
                0.0,
                gimbal_bounds["horizontal_radius_m"]
                - fixed_collision_bounds["horizontal_radius_m"],
            ),
            "bottom_offset_extension_m": max(
                0.0,
                fixed_collision_bounds["bottom_offset_m"] - gimbal_bounds["bottom_offset_m"],
            ),
            "top_offset_extension_m": max(
                0.0,
                gimbal_bounds["top_offset_m"] - fixed_collision_bounds["top_offset_m"],
            ),
        }
        gimbal_extension["extends_existing_envelope"] = any(
            gimbal_extension[key] > 1e-9
            for key in (
                "horizontal_radius_extension_m",
                "bottom_offset_extension_m",
                "top_offset_extension_m",
            )
        )
        envelope_bounds = {
            "horizontal_radius_m": max(
                envelope_bounds["horizontal_radius_m"], gimbal_bounds["horizontal_radius_m"]
            ),
            "bottom_offset_m": min(
                envelope_bounds["bottom_offset_m"], gimbal_bounds["bottom_offset_m"]
            ),
            "top_offset_m": max(envelope_bounds["top_offset_m"], gimbal_bounds["top_offset_m"]),
        }
        level_bounds = {
            "horizontal_radius_m": max(
                level_bounds["horizontal_radius_m"], gimbal_bounds["level_horizontal_radius_m"]
            ),
            "bottom_offset_m": min(
                level_bounds["bottom_offset_m"], gimbal_bounds["level_bottom_offset_m"]
            ),
            "top_offset_m": max(level_bounds["top_offset_m"], gimbal_bounds["level_top_offset_m"]),
        }
    expected_collisions = (
        collisions if gimbal_geometry is None else gimbal_geometry.collision_definitions
    )
    # These are source-declared collision points in one saved model pose, not
    # contact-force observations. No named support or ground exemption is granted.
    document = {
        "contract": CONTRACT,
        "schema_version": 1,
        "runtime_kind": "SIM",
        "vehicle_class": "multirotor",
        "image": PINNED_IMAGE,
        "reference_point": "source-derived_base_link_body_navigation_candidate",
        "model_to_body_offset_flu_m": list(offset),
        "position_formula": "body_ENU = model_ENU + R(model_FLU_to_ENU) * model_to_body_offset_FLU",
        **(
            {
                "model_sources": {k: v.to_mapping() for k, v in sorted(model_sources.items())},
                "extraction_source": extraction_source.to_mapping(),
            }
            if gimbal_geometry is None
            else {"gimbal_sources": {k: v.to_mapping() for k, v in sorted(gimbal_sources.items())}}
        ),
        "generated_sdf_source": generated_sdf_source.to_mapping(),
        "collision_count": len(expected_collisions),
        "collision_ids": sorted(expected_collisions),
        "loaded_collision_definitions_match": True,
        "rotor_sweep": (
            "each revolute collision box swept through every joint angle around body-up axis"
        ),
        "attitude_domain": {
            "maximum_body_up_tilt_deg": tilt,
            "yaw": "unrestricted",
            "runtime_tilt_bound_verified": False,
        },
        "envelope_bounds": envelope_bounds,
        "level_body_collision_bounds": level_bounds,
        "outward_numerical_padding_m": OUTWARD_PAD_M,
        "loaded_definition_comparison_tolerance": TOLERANCE,
        "loaded_pose_diagnostic": {
            "model_pose_enu": list(pose[1]),
            "body_reference_enu_m": list(loaded_body),
            "rigid_body_collision_bottom_up_m": body_bottom,
            "declared_pad_top_up_m": pad_top,
            "bottom_minus_pad_top_m": body_bottom - pad_top,
            "overlap_is_geometry_only_not_solver_contact_proof": True,
            "source_pose_is_sensor_coeval_claimed": False,
            **(
                {
                    "gimbal_camera_collision_bottom_up_m": gimbal_loaded_bottom,
                    "all_loaded_collision_bottom_up_m": min(body_bottom, gimbal_loaded_bottom),
                }
                if gimbal_geometry is not None
                else {}
            ),
        },
        "physics_declarations": {
            "world_physics_attributes": dict(_one(world, "physics").attrib),
            "world_max_step_size_text": _one(world, "physics").findtext("max_step_size"),
            "world_plugin_count": len(world.findall("plugin")),
            "vehicle_collision_contact_fields": declarations,
            "pad_collision_contact_bitmask_text": pad_collision.findtext(
                "surface/contact/collide_bitmask"
            ),
            "actual_selected_physics_engine_verified": False,
        },
        "landing_support": {
            "status": "unknown",
            "contact_collision_id": None,
            "actual_contact_forces_observed": False,
            "solver_contact_behavior_verified": False,
        },
        "full_route_clearance_ready": False,
        "source_scope": (
            "explicit three models, retained image-extraction receipt "
            "and saved generated-world reply; "
            "not image build authentication"
            if gimbal_geometry is None
            else (
                "pinned seven-binding gimbal closure, expanded loaded collisions, "
                "and source-derived joint-range sphere sweep"
            )
        ),
        **(
            {
                "gimbal_collision_range": {
                    "source_joint_limits_rad": gimbal_geometry.joint_limits,
                    "source_sphere_radius_m": gimbal_geometry.sphere_radius_m,
                    "extension_beyond_x500_collision_envelope": gimbal_extension,
                    **gimbal_bounds,
                }
            }
            if gimbal_geometry is not None
            else {}
        ),
        "claims": {
            name: False
            for name in (
                "live_asset_presence_reobserved",
                "navigation_origin_qualified",
                "actual_tilt_limit_enforced",
                "visual_mesh_or_physical_airframe_calibration",
                "collision_solver_or_contact_passed",
                "landing_support_qualified",
                "route_collision_clear",
                "flight_authority",
                "mission_approved",
                "mission_submitted",
                "flight_executed",
                "final_release_allowed",
            )
        },
    }
    if gimbal_geometry is None:
        for source in model_sources.values():
            _read_bound_sdf(root, source, identities)
        _bound_json(root, extraction_source, identities)
    else:
        for source in gimbal_geometry.source_bindings:
            _bound_bytes(root, source, MAXIMUM_SOURCE_BYTES, identities)
    _bound_bytes(root, generated_sdf_source, MAXIMUM_SOURCE_BYTES, identities)
    _require(bool(points), "rigid body collision support points missing")
    return SourceDerivedSimAirframeEnvelopeAudit(canonical_json(document))


def validate_x500_collision_envelope(audit, **inputs):
    """Recompute; a stored successful flag or caller-created object is not proof."""
    if type(audit) is not SourceDerivedSimAirframeEnvelopeAudit:
        raise TypeError("audit must be SourceDerivedSimAirframeEnvelopeAudit")
    if canonical_json(audit_x500_collision_envelope(**inputs).to_mapping()) != canonical_json(
        audit.to_mapping()
    ):
        raise AutonomyContractError(
            "airframe envelope differs from recomputed exact sources/domain"
        )


__all__ = [
    "SourceDerivedSimAirframeEnvelopeAudit",
    "audit_x500_collision_envelope",
    "validate_x500_collision_envelope",
]
