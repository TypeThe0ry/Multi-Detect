"""Functional source/geometry CPU tests; not live Gazebo or DEM acceptance."""

import hashlib
import json

import pytest

from multidetect.autonomy.onboard_executed_trajectory import EvidenceBinding
from multidetect.autonomy.serialization import AutonomyContractError
from multidetect.autonomy.sim_teacher_environment import load_static_teacher_environment

REFERENCE = """<spherical_coordinates><surface_model>EARTH_WGS84</surface_model>
<world_frame_orientation>ENU</world_frame_orientation><latitude_deg>1.3521</latitude_deg>
<longitude_deg>103.8198</longitude_deg><elevation>18</elevation></spherical_coordinates>"""
GROUND = """<model name="ground"><static>true</static><link name="base">
<collision name="ground"><geometry><plane><normal>0 0 1</normal><size>100 100</size>
</plane></geometry></collision></link></model>"""
PAD = """<model name="pad"><static>true</static><pose>0 0 .04 0 0 0</pose>
<link name="base"><collision name="pad"><geometry><cylinder><radius>5</radius>
<length>.08</length></cylinder></geometry></collision></link></model>"""
BOX = """<model name="building"><static>true</static><pose>20 15 4 0 0 .4</pose>
<link name="base"><collision name="roof"><geometry><box><size>10 8 8</size></box>
</geometry></collision></link></model>"""
VEHICLE = '<model name="x500_mono_cam_0"><static>false</static><link name="base_link"/></model>'


def _binding(root, path, raw):
    (root / path).write_bytes(raw)
    return EvidenceBinding(path, len(raw), hashlib.sha256(raw).hexdigest(), "CPU-fixture", None)


def _environment(root, *, ground=GROUND, generated_change=lambda value: value):
    xml = f'<sdf version="1.9"><world name="default">{REFERENCE}{ground}{PAD}{BOX}</world></sdf>'
    source = _binding(root, "world.sdf", xml.encode())
    generated = generated_change(xml.replace("</world>", VEHICLE + "</world>"))
    reply = ("data: " + json.dumps(generated) + "\n").encode()
    return load_static_teacher_environment(
        artifact_root=root,
        world_source=source,
        generated_sdf_source=_binding(root, "loaded.bin", reply),
        include_model_sources={},
    )


def test_actual_inventory_has_ground_pad_and_roof_bounds_without_resource_grant(tmp_path):
    environment = _environment(tmp_path)
    assert environment.sample_surface_bound(0, 0).ground_surface_up_m == 0
    assert environment.sample_surface_bound(0, 0).highest_static_surface_bound_up_m == 0.08
    assert environment.sample_surface_bound(20, 15).highest_static_surface_bound_up_m == 8
    assert environment.sample_surface_bound(-15, -15).highest_static_surface_bound_up_m == 0
    assert environment.sample_surface_bound(5, 5).highest_static_surface_bound_up_m == 0.08
    # The corner is outside the cylinder but within its AABB: overestimation is
    # deliberate, never an exact terrain/contact label.
    assert set(environment.to_mapping()["claims"].values()) == {False}
    assert len(environment.canonical_hash) == 64
    environment.revalidate_sources(tmp_path)


@pytest.mark.parametrize("point", [(50.01, 0), (0, -50.01)])
def test_queries_outside_declared_ground_domain_do_not_extrapolate(tmp_path, point):
    with pytest.raises(AutonomyContractError, match="covers"):
        _environment(tmp_path).sample_surface_bound(*point)


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), "0", 1000001])
def test_invalid_query_coordinates_rejected(tmp_path, value):
    with pytest.raises(ValueError):
        _environment(tmp_path).sample_surface_bound(value, 0)


def test_missing_ground_and_changed_loaded_world_refused(tmp_path):
    with pytest.raises(AutonomyContractError, match="ground plane"):
        _environment(tmp_path, ground="")
    with pytest.raises(AutonomyContractError, match="differs"):
        _environment(tmp_path, generated_change=lambda x: x.replace("<length>.08", "<length>.09"))


@pytest.mark.parametrize("path", ["world.sdf", "loaded.bin"])
def test_environment_reopens_original_sources_instead_of_trusting_saved_receipt(tmp_path, path):
    environment = _environment(tmp_path)
    (tmp_path / path).write_bytes((tmp_path / path).read_bytes() + b" ")
    with pytest.raises(AutonomyContractError):
        environment.revalidate_sources(tmp_path)


def test_plane_yaw_and_declared_domain_are_resolved(tmp_path):
    ground = GROUND.replace(
        '<link name="base">', '<pose>10 0 0 0 0 1.5707963267948966</pose><link name="base">'
    )
    ground = ground.replace("<size>100 100</size>", "<size>10 20</size>")
    environment = _environment(tmp_path, ground=ground)
    assert environment.sample_surface_bound(19, 4).ground_surface_up_m == 0
    with pytest.raises(AutonomyContractError, match="covers"):
        environment.sample_surface_bound(10, 6)
