"""Generated-SDF CPU fixtures, not a Gazebo execution or solver contact proof."""

from __future__ import annotations

import copy
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from multidetect.autonomy.onboard_executed_trajectory import EvidenceBinding
from multidetect.autonomy.serialization import AutonomyContractError
from multidetect.autonomy.sim_loaded_world import (
    audit_loaded_static_geometry,
    decode_generated_sdf_reply,
)

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = """<spherical_coordinates><surface_model>EARTH_WGS84</surface_model>
<world_frame_orientation>ENU</world_frame_orientation><latitude_deg>1.3521</latitude_deg>
<longitude_deg>103.8198</longitude_deg><elevation>18</elevation></spherical_coordinates>"""
MODEL = """<model name="pad"><static>true</static><pose>0 0 0 0 0 0</pose>
<link name="link"><collision name="collision"><pose>0 0 0.04 0 0 0</pose>
<geometry><cylinder><radius>5</radius><length>.08</length></cylinder></geometry>
</collision></link></model>"""
VEHICLE = '<model name="x500_mono_cam_0"><static>false</static><link name="base_link"/></model>'


def binding(root, path, raw):
    (root / path).write_bytes(raw)
    return EvidenceBinding(path, len(raw), hashlib.sha256(raw).hexdigest(), "CPU-fixture", None)


def reply(xml):
    return ("data: " + json.dumps(xml, ensure_ascii=True) + "\n").encode("ascii")


def fixture(root, mutate=lambda x: x):
    xml = f'<sdf version="1.9"><world name="default">{REFERENCE}{MODEL}</world></sdf>'
    source = binding(root, "world.sdf", xml.encode())
    generated = mutate(
        xml.replace('version="1.9"', 'version="1.10"').replace("</world>", VEHICLE + "</world>")
    )
    generated_source = binding(root, "reply.bin", reply(generated))
    return {
        "artifact_root": root,
        "world_source": source,
        "generated_sdf_source": generated_source,
        "include_model_sources": {},
    }


def test_generated_static_collision_matches_without_approving_contact_or_origin(tmp_path):
    receipt = audit_loaded_static_geometry(**fixture(tmp_path))
    mapping = receipt.to_mapping()
    assert receipt.collision_definitions_match
    assert mapping["expected_collision_count"] == mapping["generated_collision_count"] == 1
    assert mapping["excluded_dynamic_vehicle"] == "x500_mono_cam_0"
    assert mapping["differences"] == []
    assert set(mapping["claims"].values()) == {False}
    assert len(receipt.canonical_hash) == 64


@pytest.mark.parametrize(
    "changed",
    [
        lambda x: x.replace("<radius>5</radius>", "<radius>4.99</radius>"),
        lambda x: x.replace("<length>.08</length>", "<length>.07</length>"),
        lambda x: x.replace("0 0 0.04 0 0 0", "0.01 0 0.04 0 0 0"),
        lambda x: x.replace("0 0 0.04 0 0 0", "0 0 0.04 .01 0 0"),
        lambda x: x.replace("<elevation>18</elevation>", "<elevation>18.001</elevation>"),
        lambda x: x.replace('<collision name="collision">', '<collision name="wrong">'),
    ],
)
def test_changed_definitions_produce_explicit_failed_match(tmp_path, changed):
    result = audit_loaded_static_geometry(**fixture(tmp_path, changed))
    assert not result.collision_definitions_match
    assert result.to_mapping()["differences"]


@pytest.mark.parametrize(
    "changed",
    [
        lambda x: x.replace(VEHICLE, ""),
        lambda x: x.replace(VEHICLE, VEHICLE + VEHICLE),
        lambda x: x.replace(
            "</world>", '<model name="intruder"><static>true</static></model></world>'
        ),
        lambda x: x.replace(
            '<model name="pad">', '<model name="pad"><plugin name="moving" filename="moving"/>'
        ),
        lambda x: x.replace('<link name="link">', '<link name="link"><frame name="frame"/>'),
        lambda x: x.replace(
            '<world name="default">', '<world name="default"><actor name="actor"/>'
        ),
        lambda x: x.replace(
            '<world name="default">',
            '<world name="default"><include><uri>model://unresolved</uri></include>',
        ),
        lambda x: x.replace("<pose>", '<pose relative_to="world">'),
        lambda x: x.replace("<pose>", '<pose degrees="true">'),
        lambda x: x.replace("<static>true</static>", "<static>false</static>"),
        lambda x: x.replace(
            "<static>true</static>", "<static>true</static><enable_wind>true</enable_wind>"
        ),
        lambda x: x.replace('version="1.10"', 'version="1.8"'),
        lambda x: '<!DOCTYPE sdf [<!ENTITY x "foo">]>' + x,
    ],
)
def test_unknown_frames_models_dynamic_behavior_and_xml_refused(tmp_path, changed):
    with pytest.raises(AutonomyContractError):
        audit_loaded_static_geometry(**fixture(tmp_path, changed))


def test_benign_exported_defaults_and_empty_parent_frame_accepted(tmp_path):
    def changed(x):
        return (
            x.replace(
                '<model name="pad">',
                '<model name="pad"><self_collide>false</self_collide>'
                "<allow_auto_disable>true</allow_auto_disable>",
            )
            .replace('<link name="link">', '<link name="link"><enable_wind>false</enable_wind>')
            .replace("<pose>", '<pose relative_to="">')
            .replace("<static>true</static>", "<static>1</static>")
        )

    assert audit_loaded_static_geometry(**fixture(tmp_path, changed)).collision_definitions_match


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b'data: "x" data: "y"',
        b'header {} data: "x"',
        b'data: "\\u0041"',
        b'data: "\\400"',
        b'data: "\\x1"',
        b'data: "a\nb"',
        b'data: "\\q"',
        b'data: "\\000"',
        b'data: "unterminated',
        b'data: "\\"',
        b'data: "\xff"',
    ],
)
def test_only_exact_bounded_proto_string_reply_accepted(raw):
    with pytest.raises(AutonomyContractError):
        decode_generated_sdf_reply(raw)


def test_octal_hex_quotes_slashes_and_newlines_decode_without_eval():
    assert decode_generated_sdf_reply(b'data: "\\042\\134\\n\\x41\\303\\251"') == b'"\\\nA\xc3\xa9'


def test_source_drift_pointer_and_scope_refused(tmp_path):
    kwargs = fixture(tmp_path)
    (tmp_path / "reply.bin").write_bytes(b'data: "different"')
    with pytest.raises(AutonomyContractError, match="SHA"):
        audit_loaded_static_geometry(**kwargs)
    kwargs = fixture(tmp_path)
    with pytest.raises(AutonomyContractError, match="exclusion"):
        audit_loaded_static_geometry(**kwargs, vehicle_model_name="other")


def test_current_repository_urban_scene_expansion_is_CPU_only(tmp_path):
    # Reconstruct an export fixture from declared sources. This proves the
    # comparison, not that a running server loaded these files.
    world_relative = "simulation/gazebo/worlds/urban_inspection_multirotor_001.sdf"
    target_relative = "simulation/gazebo/models/multidetect_search_target_v1/model.sdf"
    world_raw, target_raw = (
        (ROOT / world_relative).read_bytes(),
        (ROOT / target_relative).read_bytes(),
    )
    world = binding(tmp_path, "world.sdf", world_raw)
    target = binding(tmp_path, "target.sdf", target_raw)
    document = ET.fromstring(world_raw)
    generated = document[0]
    include = generated.find("include")
    model = copy.deepcopy(ET.fromstring(target_raw)[0])
    model.set("name", include.find("name").text)
    model.insert(0, copy.deepcopy(include.find("pose")))
    generated.remove(include)
    generated.append(model)
    generated.append(ET.fromstring(VEHICLE))
    document.set("version", "1.11")
    source = binding(tmp_path, "reply.bin", reply(ET.tostring(document, encoding="unicode")))
    result = audit_loaded_static_geometry(
        artifact_root=tmp_path,
        world_source=world,
        generated_sdf_source=source,
        include_model_sources={"model://multidetect_search_target_v1": target},
    )
    assert result.collision_definitions_match
    assert result.to_mapping()["generated_collision_count"] == 15
