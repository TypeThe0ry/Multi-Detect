"""CPU static-geometry fixtures, not dynamic Gazebo or flight permission."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from test_sim_planning_inputs import _attestation, _context

from multidetect.autonomy.contracts import GeoPoint
from multidetect.autonomy.onboard_executed_trajectory import EvidenceBinding
from multidetect.autonomy.onboard_teacher_mission import HeldTeacherLeg
from multidetect.autonomy.serialization import AutonomyContractError
from multidetect.autonomy.sim_static_route_clearance import (
    ENVELOPE_CONTRACT,
    ORIGIN_CONTRACT,
    evaluate_static_teacher_route,
    inspect_static_gazebo_scene,
    validate_static_teacher_route_clearance,
)
from multidetect.px4_mission import MultirotorMissionGeometry
from multidetect.target_geolocation import wgs84_forward_from_ned

_ROOT = Path(__file__).resolve().parents[1]
_REFERENCE = """<spherical_coordinates><surface_model>EARTH_WGS84</surface_model>
<world_frame_orientation>ENU</world_frame_orientation><latitude_deg>1.3521</latitude_deg>
<longitude_deg>103.8198</longitude_deg><elevation>18</elevation></spherical_coordinates>"""


def _binding(root, path):
    raw = (root / path).read_bytes()
    return EvidenceBinding(
        path, len(raw), hashlib.sha256(raw).hexdigest(), "static-CPU-source", None
    )


def _world(root, models="", extras=""):
    path = root / "world.sdf"
    path.write_text(
        f'<sdf version="1.9"><world name="default">{_REFERENCE}{models}{extras}</world></sdf>',
        encoding="utf-8",
    )
    return _binding(root, "world.sdf")


def _box(
    name="box",
    pose="0 0 0 0 0 0",
    link_pose="0 0 0 0 0 0",
    collision_pose="0 0 0 0 0 0",
    size="2 4 6",
):
    return f'''<model name="{name}"><static>true</static><pose>{pose}</pose>
<link name="body"><pose>{link_pose}</pose><collision name="collision"><pose>{collision_pose}</pose>
<geometry><box><size>{size}</size></box></geometry></collision></link></model>'''


def test_actual_urban_world_and_included_target_collision_bytes_are_inspected():
    world = _binding(_ROOT, "simulation/gazebo/worlds/urban_inspection_multirotor_001.sdf")
    target = _binding(_ROOT, "simulation/gazebo/models/multidetect_search_target_v1/model.sdf")
    scene = inspect_static_gazebo_scene(
        artifact_root=_ROOT,
        world_source=world,
        include_model_sources={"model://multidetect_search_target_v1": target},
    )
    assert scene.reference_latitude_deg == 1.3521
    assert scene.reference_longitude_deg == 103.8198
    assert scene.reference_elevation_m == 18.0
    assert (
        len(scene.collisions) == 15
    )  # 7 world collision primitives + 8 included target primitives
    collisions = {c.collision_id: c for c in scene.collisions}
    assert collisions["ground_plane/ground_link/ground_collision"].kind == "plane"
    assert collisions["multirotor_home_pad/pad_link/pad_collision"].upper_surface_up_m == 0.08
    assert not any("survey_marker" in name or "tree_line" in name for name in collisions)
    target_chassis = collisions[
        "multidetect_search_target_urban_001/chassis_link/chassis_collision"
    ]
    assert target_chassis.center_enu_m == (180.0, -58.0, 0.55)
    assert target_chassis.source_sha256 == target.sha256
    alpha = collisions["inspection_block_alpha/building_link/building_collision"]
    expected_x_radius = math.cos(0.12) * 19 + math.sin(0.12) * 14
    assert alpha.minimum_enu_m[0] == pytest.approx(70 - expected_x_radius)
    assert (
        scene.canonical_hash
        == inspect_static_gazebo_scene(
            artifact_root=_ROOT,
            world_source=world,
            include_model_sources={"model://multidetect_search_target_v1": target},
        ).canonical_hash
    )


def test_composes_model_link_collision_roll_pitch_yaw_without_dropping_nested_translation(tmp_path):
    source = _world(
        tmp_path,
        _box(
            pose="10 20 30 0 0 1.5707963267948966",
            link_pose="2 0 0 0 1.5707963267948966 0",
            collision_pose="0 0 3 0 0 0",
        ),
    )
    scene = inspect_static_gazebo_scene(
        artifact_root=tmp_path, world_source=source, include_model_sources={}
    )
    box = scene.collisions[0]
    assert box.center_enu_m == pytest.approx((10, 25, 30))
    assert box.minimum_enu_m == pytest.approx((8, 22, 29))
    assert box.maximum_enu_m == pytest.approx((12, 28, 31))


@pytest.mark.parametrize(
    "changed",
    [
        lambda s: s.replace("<static>true</static>", "<static>false</static>"),
        lambda s: s.replace("<static>true</static>", ""),
        lambda s: s.replace('<link name="body">', '<frame name="unknown"/><link name="body">'),
        lambda s: s.replace(
            '<link name="body">', '<joint name="moving" type="revolute"/><link name="body">'
        ),
        lambda s: s.replace(
            '<link name="body">', '<plugin name="moving" filename="foo"/><link name="body">'
        ),
        lambda s: s.replace(
            "<geometry>", '<surface><plugin name="moving" filename="foo"/></surface><geometry>', 1
        ),
        lambda s: s.replace(
            "<pose>0 0 0 0 0 0</pose>", '<pose relative_to="other">0 0 0 0 0 0</pose>', 1
        ),
        lambda s: s.replace("<size>2 4 6</size>", "<size>2 4 6</size><mesh/>"),
        lambda s: s.replace("<box><size>2 4 6</size></box>", "<mesh><uri>unknown.obj</uri></mesh>"),
        lambda s: s.replace("<box><size>2 4 6</size></box>", "<heightmap/>"),
        lambda s: s.replace("<size>2 4 6</size>", "<size>-2 4 6</size>"),
        lambda s: s.replace("<size>2 4 6</size>", "<size>nan 4 6</size>"),
        lambda s: s.replace("<size>2 4 6</size>", "<size>inf 4 6</size>"),
        lambda s: s.replace("<size>2 4 6</size>", "<size>2 4</size>"),
    ],
)
def test_unsupported_static_geometry_and_frames_fail_closed(tmp_path, changed):
    source = _world(tmp_path, changed(_box()))
    with pytest.raises(AutonomyContractError):
        inspect_static_gazebo_scene(
            artifact_root=tmp_path, world_source=source, include_model_sources={}
        )


@pytest.mark.parametrize(
    "extras",
    [
        '<plugin filename="moving" name="moving"/>',
        '<actor name="actor"/>',
        '<frame name="world-frame"/>',
        "<include><uri>https://example.test/model</uri></include>",
        "<include><uri>model://missing</uri></include>",
    ],
)
def test_unresolved_world_models_or_dynamic_extensions_are_not_empty_space(tmp_path, extras):
    source = _world(tmp_path, _box(), extras)
    with pytest.raises(AutonomyContractError):
        inspect_static_gazebo_scene(
            artifact_root=tmp_path, world_source=source, include_model_sources={}
        )


def test_missing_or_changed_include_bytes_fail_closed(tmp_path):
    (tmp_path / "included.sdf").write_text(
        '<sdf version="1.9">' + _box(name="included") + "</sdf>", encoding="utf-8"
    )
    model = _binding(tmp_path, "included.sdf")
    source = _world(
        tmp_path, extras="<include><uri>model://included</uri><pose>5 0 0 0 0 0</pose></include>"
    )
    # A model include pose cannot silently combine with/override a model root pose.
    with pytest.raises(AutonomyContractError, match="root pose"):
        inspect_static_gazebo_scene(
            artifact_root=tmp_path,
            world_source=source,
            include_model_sources={"model://included": model},
        )
    (tmp_path / "included.sdf").write_text(
        '<sdf version="1.9">'
        + _box(name="included").replace("<pose>0 0 0 0 0 0</pose>", "", 1)
        + "</sdf>",
        encoding="utf-8",
    )
    model = _binding(tmp_path, "included.sdf")
    scene = inspect_static_gazebo_scene(
        artifact_root=tmp_path,
        world_source=source,
        include_model_sources={"model://included": model},
    )
    assert scene.collisions[0].center_enu_m == (5, 0, 0)
    (tmp_path / "included.sdf").write_bytes(b"{}")
    with pytest.raises(AutonomyContractError, match="bytes/SHA"):
        inspect_static_gazebo_scene(
            artifact_root=tmp_path,
            world_source=source,
            include_model_sources={"model://included": model},
        )


def test_empty_scene_duplicate_collision_and_extra_include_binding_denied(tmp_path):
    source = _world(tmp_path)
    with pytest.raises(AutonomyContractError, match="empty"):
        inspect_static_gazebo_scene(
            artifact_root=tmp_path, world_source=source, include_model_sources={}
        )
    source = _world(tmp_path, _box() + _box())
    with pytest.raises(AutonomyContractError, match="duplicated"):
        inspect_static_gazebo_scene(
            artifact_root=tmp_path, world_source=source, include_model_sources={}
        )
    source = _world(tmp_path, _box())
    with pytest.raises(AutonomyContractError, match="extra include"):
        inspect_static_gazebo_scene(
            artifact_root=tmp_path,
            world_source=source,
            include_model_sources={"model://unreferenced": source},
        )


def test_source_hash_and_size_and_XML_entities_are_checked(tmp_path):
    source = _world(tmp_path, _box())
    for corrupted in (
        replace(source, sha256="f" * 64),
        replace(source, bytes=source.bytes + 1),
        replace(source, bytes=1024 * 1024 + 1),
    ):
        with pytest.raises(AutonomyContractError):
            inspect_static_gazebo_scene(
                artifact_root=tmp_path, world_source=corrupted, include_model_sources={}
            )
    (tmp_path / "world.sdf").write_text(
        '<!DOCTYPE sdf [<!ENTITY x "0">]><sdf version="1.9"/>', encoding="utf-8"
    )
    source = _binding(tmp_path, "world.sdf")
    with pytest.raises(AutonomyContractError, match="entities"):
        inspect_static_gazebo_scene(
            artifact_root=tmp_path, world_source=source, include_model_sources={}
        )


@pytest.mark.parametrize("token", ["1_0", "２", "٢", "0x2", "2,0", "−2", "2\u00a0"])
@pytest.mark.parametrize("field", ["size", "pose", "latitude_deg", "elevation"])
def test_nonportable_SDF_numeric_lexemes_fail_closed(tmp_path, token, field):
    body = _box(size=f"{token} 4 6") if field == "size" else _box()
    if field == "pose":
        body = _box(pose=f"{token} 0 0 0 0 0")
    source = _world(tmp_path, body)
    if field in {"latitude_deg", "elevation"}:
        path = tmp_path / "world.sdf"
        before = "1.3521" if field == "latitude_deg" else "18"
        path.write_text(
            path.read_text().replace(f"<{field}>{before}</{field}>", f"<{field}>{token}</{field}>"),
            encoding="utf-8",
        )
        source = _binding(tmp_path, "world.sdf")
    with pytest.raises(AutonomyContractError):
        inspect_static_gazebo_scene(
            artifact_root=tmp_path, world_source=source, include_model_sources={}
        )


def test_ASCII_scientific_numbers_and_XML_whitespace_have_explicit_semantics(tmp_path):
    source = _world(tmp_path, _box(size=" \t+2e0\n4.0\r6E+0 ", pose=".5 -1e-1 0.0 0 0 0"))
    scene = inspect_static_gazebo_scene(
        artifact_root=tmp_path, world_source=source, include_model_sources={}
    )
    assert scene.collisions[0].center_enu_m == (0.5, -0.1, 0.0)
    assert scene.collisions[0].half_extents_local_m == (1.0, 2.0, 3.0)


def _json_source(root, path, value):
    (root / path).write_text(json.dumps(value), encoding="utf-8")
    return replace(_binding(root, path), json_pointer="")


def _route_setup(
    root, waypoints=((0, 8, 12), (0, 0, 12)), *, origin_mutator=None, envelope_mutator=None
):
    shutil.copyfile(
        _ROOT / "simulation/gazebo/worlds/urban_inspection_multirotor_001.sdf", root / "world.sdf"
    )
    shutil.copyfile(
        _ROOT / "simulation/gazebo/models/multidetect_search_target_v1/model.sdf",
        root / "target.sdf",
    )
    shutil.copyfile(
        _ROOT / "simulation/gazebo/models/x500_mono_cam/model.sdf", root / "x500-composition.sdf"
    )
    world, target = _binding(root, "world.sdf"), _binding(root, "target.sdf")
    context = _context(
        aircraft_position=GeoPoint(1.3521, 103.8198, 0, "relative_home"),
        home_position=GeoPoint(1.3521, 103.8198),
    )
    attestation = _attestation(scenario_id="urban_inspection_multirotor_001")
    evidence = [
        _json_source(root, "fixture-home.json", {"CPU_fixture": "home packet"}).to_mapping(),
        _json_source(
            root, "fixture-pose.json", {"CPU_fixture": "navigation reference pose"}
        ).to_mapping(),
        _json_source(
            root, "fixture-extrinsic-reset.json", {"CPU_fixture": "extrinsic and reset"}
        ).to_mapping(),
    ]
    origin = {
        "contract": ORIGIN_CONTRACT,
        "schema_version": 1,
        "runtime_kind": "SIM",
        "vehicle_class": "multirotor",
        "observed_context_sha256": context.canonical_hash,
        "airframe_profile_id": context.airframe_profile_id,
        "airframe_profile_sha256": context.airframe_profile_hash,
        "runtime_attestation_sha256": attestation.canonical_hash,
        "container_id": attestation.container_id,
        "image": attestation.image,
        "scenario_id": attestation.scenario_id,
        "scenario_manifest_sha256": attestation.scenario_manifest_sha256,
        "world_source": world.to_mapping(),
        "world_reference": {
            "latitude_deg": 1.3521,
            "longitude_deg": 103.8198,
            "elevation_m": 18,
            "altitude_datum": "SDF_world_elevation",
            "world_frame": "ENU",
            "heading_rad": 0,
        },
        "reference_point": "PX4_navigation_reference_point",
        "verified_home_enu_m": [0, 0, 0.28],
        "home_position": context.home_position.to_mapping(),
        "clock_id": "fixture-clock",
        "observed_at_s": 10.5,
        "qualified_at_s": 11,
        "expires_at_s": 12,
        "horizontal_uncertainty_m": 0.02,
        "vertical_uncertainty_m": 0.02,
        "source_evidence": evidence,
        "verification": {
            "PX4_navigation_reference_point_identified": True,
            "Gazebo_world_ENU_pose_observed": True,
            "pose_to_navigation_reference_extrinsic_bound": True,
            "EKF_reset_continuity_verified": True,
            "WGS84_home_mapping_checked": True,
        },
    }
    envelope = {
        "contract": ENVELOPE_CONTRACT,
        "schema_version": 1,
        "runtime_kind": "SIM",
        "vehicle_class": "multirotor",
        "image": attestation.image,
        "airframe_profile_id": context.airframe_profile_id,
        "airframe_profile_sha256": context.airframe_profile_hash,
        "reference_point": "PX4_navigation_reference_point",
        "horizontal_radius_m": 0.45,
        "bottom_offset_m": -0.2,
        "top_offset_m": 0.4,
        "horizontal_tracking_margin_m": 0.1,
        "vertical_tracking_margin_m": 0.1,
        "gps_horizontal_accuracy_multiplier": 1,
        "projection_margin_m": 0.05,
        "contact_tolerance_m": 0.02,
        "contact_collision_id": "multirotor_home_pad/pad_link/pad_collision",
        "model_sources": [_binding(root, "x500-composition.sdf").to_mapping()],
        "provenance": "CPU fixture envelope only; actual x500 dependencies/extrinsic not certified",
        "physical_calibration_claimed": False,
    }
    if origin_mutator:
        origin_mutator(origin)
    if envelope_mutator:
        envelope_mutator(envelope)
    points = []
    for east, north, up in waypoints:
        latitude, longitude = wgs84_forward_from_ned(
            latitude_deg=1.3521, longitude_deg=103.8198, north_offset_m=north, east_offset_m=east
        )
        points.append(GeoPoint(latitude, longitude, up, "relative_home"))
    geometry = MultirotorMissionGeometry(
        home=context.home_position,
        waypoints=tuple(points),
        takeoff_altitude_m=12,
        loiter_time_s=2,
        loiter_radius_m=0.5,
        acceptance_radius_m=0.5,
    )
    return dict(
        artifact_root=root,
        world_source=world,
        include_model_sources={"model://multidetect_search_target_v1": target},
        origin_source=_json_source(root, "origin.json", origin),
        airframe_envelope_source=_json_source(root, "envelope.json", envelope),
        observed_context=context,
        geometry=geometry,
        legs=tuple(HeldTeacherLeg(i + 1, 2, i * 90) for i in range(len(points))),
        runtime_attestation=attestation,
        now_s=11,
        clock_id="fixture-clock",
    )


def test_complete_static_route_clears_ground_contact_without_whole_world_ground_exemption(tmp_path):
    options = _route_setup(tmp_path)
    original_mapping = options["observed_context"].to_mapping()
    receipt = evaluate_static_teacher_route(**options)
    assert receipt.route_collision_clear
    assert receipt.collision_hits == ()
    assert receipt.observed_context_sha256 == options["observed_context"].canonical_hash
    assert options["observed_context"].to_mapping() == original_mapping
    assert not options["observed_context"].camera_available
    assert not options["observed_context"].geofence_valid
    assert receipt.to_mapping()["collision_count"] == 15
    phases = {interval["phase"] for interval in receipt.to_mapping()["audited_intervals"]}
    assert phases == {
        "takeoff",
        "transit-1",
        "transit-2",
        "hold-1",
        "hold-2",
        "terminal_loiter",
        "landing",
    }
    exceptions = receipt.to_mapping()["contact_phase_exceptions"]
    assert {item["phase"] for item in exceptions} == {"takeoff", "landing"}
    assert {item["collision_id"] for item in exceptions} == {
        "ground_plane/ground_link/ground_collision",
        "multirotor_home_pad/pad_link/pad_collision",
        "inspection_road/road_link/road_collision",
    }
    assert all(claim is False for claim in receipt.to_mapping()["claims"].values())
    assert evaluate_static_teacher_route(**options).canonical_hash == receipt.canonical_hash


def test_mid_segment_collision_is_not_hidden_by_clear_waypoint_endpoints(tmp_path):
    options = _route_setup(tmp_path, ((30, 35, 12), (105, 35, 12), (0, 0, 12)))
    receipt = evaluate_static_teacher_route(**options)
    assert not receipt.route_collision_clear
    assert (
        "transit-2",
        "inspection_block_alpha/building_link/building_collision",
    ) in receipt.collision_hits
    assert not any(
        phase.startswith("hold-") and "block_alpha" in name
        for phase, name in receipt.collision_hits
    )


def test_height_and_airframe_vertical_margin_are_applied_to_roof_and_near_ground_hold(tmp_path):
    near_roof = _route_setup(tmp_path, ((70, 35, 14.7), (0, 0, 12)))
    assert not evaluate_static_teacher_route(**near_roof).route_collision_clear
    high_roof = _route_setup(tmp_path, ((70, 35, 16), (0, 0, 16)))
    assert evaluate_static_teacher_route(**high_roof).route_collision_clear
    low_hold = _route_setup(tmp_path, ((0, 8, 0.1), (0, 0, 12)))
    receipt = evaluate_static_teacher_route(**low_hold)
    assert ("hold-1", "ground_plane/ground_link/ground_collision") in receipt.collision_hits


@pytest.mark.parametrize(
    "mutation",
    [
        lambda o: o["verification"].update(EKF_reset_continuity_verified=False),
        lambda o: o["verification"].update(pose_to_navigation_reference_extrinsic_bound=False),
        lambda o: o.update(reference_point="Gazebo_model_origin"),
        lambda o: o.update(verified_home_enu_m=[0, 0, 0]),
        lambda o: o.update(verified_home_enu_m=[0, 0, 0.27]),
        lambda o: o.update(verified_home_enu_m=[0, 0, 0.35]),
        lambda o: o.update(observed_context_sha256="e" * 64),
        lambda o: o.update(runtime_attestation_sha256="e" * 64),
        lambda o: o.update(airframe_profile_sha256="e" * 64),
        lambda o: o.update(image="other@sha256:" + "e" * 64),
        lambda o: o.update(scenario_id="other"),
        lambda o: o.update(source_evidence=[]),
        lambda o: o["world_reference"].update(world_frame="NED"),
        lambda o: o["world_reference"].update(heading_rad=0.1),
        lambda o: o["world_reference"].update(heading_rad=False),
        lambda o: o["world_reference"].update(altitude_datum="guessed_MSL"),
        lambda o: o["world_reference"].update(latitude_deg=47.39),
    ],
)
def test_unknown_mismatched_reset_or_guessed_origin_never_becomes_clearance(tmp_path, mutation):
    options = _route_setup(tmp_path, origin_mutator=mutation)
    with pytest.raises(AutonomyContractError):
        evaluate_static_teacher_route(**options)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda e: e.update(physical_calibration_claimed=True),
        lambda e: e.update(horizontal_radius_m=0),
        lambda e: e.update(bottom_offset_m=0.1),
        lambda e: e.update(top_offset_m=float("nan")),
        lambda e: e.update(gps_horizontal_accuracy_multiplier=0.5),
        lambda e: e.update(vertical_tracking_margin_m=0),
        lambda e: e.update(projection_margin_m=0),
        lambda e: e.update(contact_tolerance_m=1),
        lambda e: e.update(contact_collision_id="nonexistent"),
        lambda e: e.update(contact_collision_id="ground_plane/ground_link/ground_collision"),
        lambda e: e.update(model_sources=[]),
        lambda e: e.update(reference_point="x500_model_origin"),
    ],
)
def test_unbound_or_nonconservative_envelope_declarations_deny(tmp_path, mutation):
    options = _route_setup(tmp_path, envelope_mutator=mutation)
    with pytest.raises(AutonomyContractError):
        evaluate_static_teacher_route(**options)


def test_home_contact_footprint_includes_GPS_acceptance_and_airframe_margins(tmp_path):
    options = _route_setup(tmp_path, envelope_mutator=lambda e: e.update(horizontal_radius_m=4))
    with pytest.raises(AutonomyContractError, match="footprint"):
        evaluate_static_teacher_route(**options)


@pytest.mark.parametrize(
    "field,value", [("now_s", 9.0), ("now_s", 12.1), ("clock_id", "other-clock")]
)
def test_old_future_or_wrong_clock_origin_is_not_live_clearance(tmp_path, field, value):
    options = _route_setup(tmp_path)
    options[field] = value
    with pytest.raises(AutonomyContractError):
        evaluate_static_teacher_route(**options)


@pytest.mark.parametrize(
    "path",
    [
        "world.sdf",
        "target.sdf",
        "origin.json",
        "envelope.json",
        "fixture-pose.json",
        "x500-composition.sdf",
    ],
)
def test_changed_world_include_origin_pose_and_envelope_sources_deny(tmp_path, path):
    options = _route_setup(tmp_path)
    evaluate_static_teacher_route(**options)
    (tmp_path / path).write_bytes(b"{}")
    with pytest.raises(AutonomyContractError, match="bytes/SHA"):
        evaluate_static_teacher_route(**options)


def test_no_guessed_hidden_land_approach_or_takeoff_ground_penetration(tmp_path):
    options = _route_setup(tmp_path, ((0, 8, 12), (0, 16, 12)))
    with pytest.raises(AutonomyContractError, match="elevated home-return"):
        evaluate_static_teacher_route(**options)
    options = _route_setup(tmp_path)
    original = options["observed_context"]
    changed = replace(
        original, aircraft_position=replace(original.aircraft_position, altitude_m=-0.01)
    )
    origin = json.loads((tmp_path / "origin.json").read_text())
    origin["observed_context_sha256"] = changed.canonical_hash
    options.update(
        observed_context=changed, origin_source=_json_source(tmp_path, "origin.json", origin)
    )
    with pytest.raises(AutonomyContractError, match="takeoff reference penetrates"):
        evaluate_static_teacher_route(**options)


def _change_original_context(root, options, context):
    origin = json.loads((root / "origin.json").read_text())
    origin["observed_context_sha256"] = context.canonical_hash
    options.update(
        observed_context=context, origin_source=_json_source(root, "origin.json", origin)
    )


def _append_scene_collision(root, options, model):
    path = root / "world.sdf"
    path.write_text(path.read_text().replace("</world>", model + "</world>"), encoding="utf-8")
    world = _binding(root, "world.sdf")
    origin = json.loads((root / "origin.json").read_text())
    origin["world_source"] = world.to_mapping()
    options.update(world_source=world, origin_source=_json_source(root, "origin.json", origin))


def test_observed_takeoff_horizontal_offset_is_kept_independently_of_GPS_uncertainty(tmp_path):
    options = _route_setup(tmp_path)
    original = options["observed_context"]
    latitude, longitude = wgs84_forward_from_ned(
        latitude_deg=1.3521, longitude_deg=103.8198, north_offset_m=0, east_offset_m=0.9
    )
    changed = replace(original, aircraft_position=GeoPoint(latitude, longitude, 0, "relative_home"))
    _change_original_context(tmp_path, options, changed)
    receipt = evaluate_static_teacher_route(**options)
    assert receipt.route_collision_clear
    start = receipt.to_mapping()["trajectory"][0]["position_enu_m"]
    assert start[0] == pytest.approx(0.9, abs=0.0001)
    assert start[1] == pytest.approx(0, abs=0.0001)
    # 0.9 m is a nominal measured offset; the independent 1 m GPS error,
    # airframe, tracking, origin, projection and acceptance are still added.
    assert receipt.to_mapping()["horizontal_inflation_m"] == pytest.approx(2.12)
    obstacle = (
        _box(name="start_offset_obstacle")
        .replace("<pose>0 0 0 0 0 0</pose>", "<pose>3 0 0.5 0 0 0</pose>", 1)
        .replace("<size>2 4 6</size>", "<size>0.2 0.2 0.2</size>")
    )
    _append_scene_collision(tmp_path, options, obstacle)
    receipt = evaluate_static_teacher_route(**options)
    assert ("takeoff", "start_offset_obstacle/body/collision") in receipt.collision_hits


def test_detached_low_platform_is_not_an_automatic_ground_contact_exception(tmp_path):
    options = _route_setup(tmp_path)
    detached = (
        _box(name="detached_low_platform")
        .replace("<pose>0 0 0 0 0 0</pose>", "<pose>1.8 0 0.03 0 0 0</pose>", 1)
        .replace("<size>2 4 6</size>", "<size>0.2 0.2 0.06</size>")
    )
    _append_scene_collision(tmp_path, options, detached)
    receipt = evaluate_static_teacher_route(**options)
    assert not receipt.route_collision_clear
    assert ("takeoff", "detached_low_platform/body/collision") in receipt.collision_hits
    assert not any(
        "detached_low_platform" in item["collision_id"]
        for item in receipt.to_mapping()["contact_phase_exceptions"]
    )
    assert all(
        item["basis"] == "contains_contact_footprint_at_or_below_verified_support"
        for item in receipt.to_mapping()["contact_phase_exceptions"]
    )


def test_observed_start_footprint_not_only_home_footprint_must_fit_contact_support(tmp_path):
    options = _route_setup(tmp_path, envelope_mutator=lambda e: e.update(horizontal_radius_m=1.6))
    original = options["observed_context"]
    latitude, longitude = wgs84_forward_from_ned(
        latitude_deg=1.3521, longitude_deg=103.8198, north_offset_m=0, east_offset_m=0.9
    )
    _change_original_context(
        tmp_path,
        options,
        replace(original, aircraft_position=GeoPoint(latitude, longitude, 0, "relative_home")),
    )
    with pytest.raises(AutonomyContractError, match="footprint"):
        evaluate_static_teacher_route(**options)


def _validate_options(options):
    return {
        key: options[key]
        for key in (
            "observed_context",
            "geometry",
            "legs",
            "runtime_attestation",
            "artifact_root",
            "now_s",
            "clock_id",
        )
    }


def test_receipt_recomputation_reopens_sources_without_renewing_original_window(tmp_path):
    options = _route_setup(tmp_path)
    receipt = evaluate_static_teacher_route(**options)
    validate_static_teacher_route_clearance(receipt, **_validate_options(options))
    options["now_s"] = 11.9
    validate_static_teacher_route_clearance(receipt, **_validate_options(options))
    assert receipt.to_mapping()["evaluated_at_s"] == 11
    assert receipt.to_mapping()["expires_at_s"] == 12
    options["now_s"] = 12
    with pytest.raises(AutonomyContractError, match="future/stale"):
        validate_static_teacher_route_clearance(receipt, **_validate_options(options))
    options["now_s"] = 11
    (tmp_path / "fixture-pose.json").write_bytes(b"{}")
    with pytest.raises(AutonomyContractError, match="bytes/SHA"):
        validate_static_teacher_route_clearance(receipt, **_validate_options(options))


@pytest.mark.parametrize(
    "key,value",
    [
        ("route_collision_clear", True),
        ("horizontal_inflation_m", 0),
        ("claims", {"execution_authority": True}),
    ],
)
def test_forged_clearance_or_margin_receipt_never_bypasses_full_recomputation(tmp_path, key, value):
    options = _route_setup(tmp_path, ((30, 35, 12), (105, 35, 12), (0, 0, 12)))
    receipt = evaluate_static_teacher_route(**options)
    assert not receipt.route_collision_clear
    document = json.loads(receipt._details_json)
    document[key] = value
    forged = replace(receipt, _details_json=json.dumps(document))
    with pytest.raises(AutonomyContractError, match="recomputed"):
        validate_static_teacher_route_clearance(forged, **_validate_options(options))


def test_receipt_recomputation_refuses_changed_route_or_observation(tmp_path):
    options = _route_setup(tmp_path)
    receipt = evaluate_static_teacher_route(**options)
    options["geometry"] = replace(options["geometry"], takeoff_altitude_m=13)
    with pytest.raises(AutonomyContractError, match="recomputed"):
        validate_static_teacher_route_clearance(receipt, **_validate_options(options))
    options = _route_setup(tmp_path)
    receipt = evaluate_static_teacher_route(**options)
    options["observed_context"] = replace(options["observed_context"], battery_remaining_pct=89)
    with pytest.raises(AutonomyContractError, match="original observed"):
        validate_static_teacher_route_clearance(receipt, **_validate_options(options))
