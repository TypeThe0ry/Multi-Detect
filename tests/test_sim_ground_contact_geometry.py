"""Signed geometry software fixtures; not live contact or flight evidence."""

from __future__ import annotations

import copy
import math
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest
from test_sim_airframe_envelope import _binding, _change_model, _fixture, _write_reply
from test_sim_teacher_environment import GROUND, REFERENCE

from multidetect.autonomy import sim_ground_contact_geometry as api
from multidetect.autonomy.serialization import AutonomyContractError, canonical_json
from multidetect.autonomy.sim_airframe_envelope import decode_generated_sdf_reply
from multidetect.autonomy.sim_teacher_environment import load_static_teacher_environment

SUPPORT = "multirotor_home_pad/pad_link/pad_collision"


def _inputs(root, *, model_pose="0 0 .06699981651620895 0 0 0", change=None):
    inputs = _fixture(root)
    generated = ET.fromstring(decode_generated_sdf_reply((root / "reply.bin").read_bytes()))
    world = generated[0]
    world.append(ET.fromstring(REFERENCE))
    world.append(ET.fromstring(GROUND))
    pad = next(m for m in world.findall("model") if m.attrib["name"] == "multirotor_home_pad")
    ET.SubElement(pad, "static").text = "true"
    vehicle = next(m for m in world.findall("model") if m.attrib["name"] == "x500_mono_cam_0")
    vehicle.find("pose").text = model_pose
    if change:
        change(world, vehicle, pad)
    _write_reply(root, generated)
    declared = copy.deepcopy(generated)
    declared.attrib["version"] = "1.9"
    declared[0].remove(
        next(m for m in declared[0].findall("model") if m.attrib["name"] == "x500_mono_cam_0")
    )
    (root / "world.sdf").write_bytes(ET.tostring(declared))
    generated_source = _binding(root, "reply.bin")
    environment = load_static_teacher_environment(
        artifact_root=root,
        world_source=_binding(root, "world.sdf"),
        generated_sdf_source=generated_source,
        include_model_sources={},
    )
    return dict(
        artifact_root=root,
        environment=environment,
        model_sources=inputs["model_sources"],
        extraction_source=inputs["extraction_source"],
        support_collision_id=SUPPORT,
    )


def test_small_signed_residual_compatible_without_contact_or_authority_claims(tmp_path):
    inputs = _inputs(tmp_path)
    result = api.audit_signed_ground_contact_geometry(**inputs)
    row = result.to_mapping()
    assert result.geometry_compatible is True
    assert row["refusal_reasons"] == []
    assert row["signed_bottom_minus_support_up_m"] == pytest.approx(-1.834837911e-7)
    assert row["predeclared_interval_m"] == [-1e-6, 0.001]
    assert row["all_orientation_footprint_radius_m"] == pytest.approx(0.3906460712965413)
    assert row["support_contains_conservative_footprint"] is True
    assert not any(row["claims"].values())
    assert row["future_ground_tilt_enforcement_verified"] is False
    assert row["contact_force_or_solver_measurements_required_by_geometry_contract"] is False
    row["geometry_compatible"] = False
    assert result.geometry_compatible is True
    api.validate_signed_ground_contact_geometry(result, **inputs)


@pytest.mark.parametrize(
    "pose,gap,reason",
    [
        ("0 0 .003 0 0 0", -0.064, "penetration_exceeds_predeclared_numerical_allowance"),
        ("0 0 .0669989 0 0 0", -1.1e-6, "penetration_exceeds_predeclared_numerical_allowance"),
        ("0 0 .0681 0 0 0", 0.0011, "positive_gap_exceeds_predeclared_contact_geometry_allowance"),
    ],
)
def test_old_large_penetration_and_signed_interval_failures_are_retained_refusals(
    tmp_path, pose, gap, reason
):
    row = api.audit_signed_ground_contact_geometry(
        **_inputs(tmp_path, model_pose=pose)
    ).to_mapping()
    assert row["geometry_compatible"] is False
    assert row["signed_bottom_minus_support_up_m"] == pytest.approx(gap)
    assert reason in row["refusal_reasons"]
    assert not any(row["claims"].values())


@pytest.mark.parametrize("pose", ["0 0 .0669991 0 0 0", "0 0 .0679 0 0 0"])
def test_predeclared_interval_accepts_small_penetration_and_small_positive_gap(tmp_path, pose):
    assert api.audit_signed_ground_contact_geometry(
        **_inputs(tmp_path, model_pose=pose)
    ).geometry_compatible


def test_pose_outside_small_ground_tilt_domain_is_not_saved_as_compatible(tmp_path):
    row = api.audit_signed_ground_contact_geometry(
        **_inputs(tmp_path, model_pose="0 0 .067 0 .035 0")
    ).to_mapping()
    assert row["saved_body_up_tilt_deg"] == pytest.approx(math.degrees(0.035))
    assert "saved_body_tilt_outside_explicit_ground_domain" in row["refusal_reasons"]
    assert row["geometry_compatible"] is False


@pytest.mark.parametrize("value", [True, False, None, float("nan"), float("inf"), -1, 5.1, 180])
def test_ground_tilt_domain_cannot_be_unbounded_or_ill_typed(tmp_path, value):
    with pytest.raises((TypeError, ValueError)):
        api.audit_signed_ground_contact_geometry(**_inputs(tmp_path), maximum_ground_tilt_deg=value)


def test_full_rotor_sweep_not_rigid_only_bottom_is_used(tmp_path):
    def change(world, vehicle, pad):
        for rotor in vehicle.findall("link")[1:5]:
            values = rotor.find("pose").text.split()
            values[2] = "-.01"
            rotor.find("pose").text = " ".join(values)

    inputs = _inputs(tmp_path, model_pose="0 0 .067 0 0 0", change=change)

    def moved_rotors(base):
        for rotor in base.findall("link")[1:5]:
            values = rotor.find("pose").text.split()
            values[2] = "-.25"
            rotor.find("pose").text = " ".join(values)

    temporary = {
        "artifact_root": tmp_path,
        "model_sources": inputs["model_sources"],
        "extraction_source": inputs["extraction_source"],
    }
    _change_model(temporary, "x500_base", moved_rotors)
    inputs["extraction_source"] = temporary["extraction_source"]
    row = api.audit_signed_ground_contact_geometry(**inputs).to_mapping()
    assert row["source_derived_rigid_collision_bottom_up_m"] == pytest.approx(0.08)
    assert row["source_derived_all_collision_bottom_up_m"] < 0.06
    assert row["geometry_compatible"] is False


def test_small_pad_does_not_contain_conservative_all_orientation_footprint(tmp_path):
    def change(world, vehicle, pad):
        pad.find("link/collision/geometry/cylinder/radius").text = ".38"

    row = api.audit_signed_ground_contact_geometry(**_inputs(tmp_path, change=change)).to_mapping()
    assert row["support_contains_conservative_footprint"] is False
    assert "support_does_not_contain_all_orientation_footprint" in row["refusal_reasons"]


def test_source_pad_and_generated_pad_both_moved_cannot_be_ignored(tmp_path):
    def change(world, vehicle, pad):
        pad.find("pose").text = "10 0 .04 0 0 0"

    assert not api.audit_signed_ground_contact_geometry(
        **_inputs(tmp_path, change=change)
    ).geometry_compatible


def test_declared_finite_plane_does_not_extend_to_infinite_support(tmp_path):
    inputs = _inputs(tmp_path, model_pose="100 0 -.013 0 0 0")
    inputs["support_collision_id"] = "ground/base/ground"
    row = api.audit_signed_ground_contact_geometry(**inputs).to_mapping()
    assert abs(row["signed_bottom_minus_support_up_m"]) < 1e-10
    assert row["support_contains_conservative_footprint"] is False
    assert row["geometry_compatible"] is False


def test_level_box_support_is_checked_in_its_rotated_declared_frame(tmp_path):
    def change(world, vehicle, pad):
        box_support = copy.deepcopy(pad)
        box_support.set("name", "box_support")
        box_support.find("pose").text = "0 0 .04 0 0 .5"
        geometry = box_support.find("link/collision/geometry")
        geometry.remove(geometry[0])
        shape = ET.SubElement(geometry, "box")
        ET.SubElement(shape, "size").text = "2 2 .08"
        world.append(box_support)

    inputs = _inputs(tmp_path, change=change)
    inputs["support_collision_id"] = "box_support/pad_link/pad_collision"
    assert api.audit_signed_ground_contact_geometry(**inputs).geometry_compatible


def test_sloped_support_has_no_guessed_horizontal_top(tmp_path):
    def change(world, vehicle, pad):
        pad.find("pose").text = "0 0 .04 0 .1 0"

    with pytest.raises(AutonomyContractError, match="level cylinder"):
        api.audit_signed_ground_contact_geometry(**_inputs(tmp_path, change=change))


def test_missing_support_and_forged_environment_refused(tmp_path):
    inputs = _inputs(tmp_path)
    with pytest.raises(AutonomyContractError, match="named support absent"):
        api.audit_signed_ground_contact_geometry(**{**inputs, "support_collision_id": "missing"})
    inputs["environment"] = replace(inputs["environment"], loaded_geometry_audit_sha256="0" * 64)
    with pytest.raises(AutonomyContractError, match="complete source recomputation"):
        api.audit_signed_ground_contact_geometry(**inputs)


@pytest.mark.parametrize("path", ["world.sdf", "reply.bin", "x500_base.sdf", "extraction.json"])
def test_full_source_closed_revalidation_detects_late_bytes_drift(tmp_path, monkeypatch, path):
    inputs = _inputs(tmp_path)
    original = api._loaded

    def drift(*args):
        value = original(*args)
        source = tmp_path / path
        source.write_bytes(source.read_bytes() + b" ")
        return value

    monkeypatch.setattr(api, "_loaded", drift)
    with pytest.raises(AutonomyContractError):
        api.audit_signed_ground_contact_geometry(**inputs)


def test_whole_call_stat_identity_not_just_same_hash_is_preserved(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path)
    original = api._loaded

    def drift(*args):
        value = original(*args)
        source = tmp_path / "world.sdf"
        stat = source.stat()
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        return value

    monkeypatch.setattr(api, "_loaded", drift)
    with pytest.raises(AutonomyContractError, match="identity/bytes/SHA"):
        api.audit_signed_ground_contact_geometry(**inputs)


def test_non_json_equivalent_claim_type_cannot_pass_validation(tmp_path):
    inputs = _inputs(tmp_path)
    row = api.audit_signed_ground_contact_geometry(**inputs).to_mapping()
    row["claims"]["ARM_sent"] = 0
    forged = api.SignedGroundContactGeometryAudit(canonical_json(row))
    with pytest.raises(AutonomyContractError, match="differs from source recomputation"):
        api.validate_signed_ground_contact_geometry(forged, **inputs)


def test_source_model_keys_cannot_omit_or_add_an_attachment(tmp_path):
    inputs = _inputs(tmp_path)
    inputs["model_sources"].pop("x500")
    with pytest.raises(AutonomyContractError, match="exact three-model"):
        api.audit_signed_ground_contact_geometry(**inputs)


def test_no_optional_runtime_imported_in_fresh_process():
    source = Path(__file__).resolve().parents[1] / "src"
    code = (
        f"import sys;sys.path.insert(0,{str(source)!r});"
        "import multidetect.autonomy.sim_ground_contact_geometry;"
        "assert not(set(sys.modules)&{'torch','PIL','numpy','pymavlink','transformers'})"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code], capture_output=True, timeout=15
    )
    assert result.returncode == 0, result.stderr.decode()
