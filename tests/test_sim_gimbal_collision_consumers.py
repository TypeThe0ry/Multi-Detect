"""Pinned-source gimbal collision consumer tests; no Gazebo or flight runtime."""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET

import pytest
from gimbal_collision_fixtures import gimbal_contact_environment, gimbal_reply, gimbal_sources

from multidetect.autonomy import sim_ground_contact_geometry as contact_api
from multidetect.autonomy.onboard_executed_trajectory import EvidenceBinding
from multidetect.autonomy.serialization import AutonomyContractError
from multidetect.autonomy.sim_airframe_envelope import (
    audit_x500_collision_envelope,
    decode_generated_sdf_reply,
)
from multidetect.autonomy.sim_gimbal_collision import load_gimbal_collision_geometry

SUPPORT = "multirotor_home_pad/pad_link/pad_collision"


def _envelope_inputs(root):
    sources = gimbal_sources(root)
    generated = gimbal_reply(root, sources)
    return {
        "artifact_root": root,
        "gimbal_sources": sources,
        "generated_sdf_source": generated,
    }


def test_real_gimbal_collision_expands_loaded_sources_and_sweeps_joint_limits(tmp_path):
    inputs = _envelope_inputs(tmp_path)
    result = audit_x500_collision_envelope(**inputs)
    row = result.to_mapping()
    geometry = load_gimbal_collision_geometry(tmp_path, inputs["gimbal_sources"], {})
    assert row["collision_count"] == 10
    assert row["loaded_collision_definitions_match"] is True
    assert row["gimbal_collision_range"]["source_joint_limits_rad"] == geometry.joint_limits
    assert row["gimbal_collision_range"]["source_sphere_radius_m"] == pytest.approx(0.035)
    assert row["gimbal_collision_range"]["joint_range_sample_step_deg"] == 1.0
    assert row["gimbal_collision_range"]["joint_range_cover_pad_m"] < 0.005
    assert row["gimbal_collision_range"]["center_min_up_m"] < -0.14
    assert row["gimbal_collision_range"]["horizontal_radius_m"] == pytest.approx(0.18723240271836)
    assert row["gimbal_collision_range"]["extension_beyond_x500_collision_envelope"] == {
        "horizontal_radius_extension_m": 0.0,
        "bottom_offset_extension_m": 0.0,
        "top_offset_extension_m": 0.0,
        "extends_existing_envelope": False,
    }
    assert row["envelope_bounds"]["horizontal_radius_m"] == pytest.approx(0.3906460712965)
    assert row["envelope_bounds"]["bottom_offset_m"] == pytest.approx(-0.3906460712965)
    assert row["loaded_pose_diagnostic"]["gimbal_camera_collision_bottom_up_m"] == pytest.approx(
        0.066
    )
    assert not any(row["claims"].values())


def test_gimbal_joint_range_bound_contains_independent_interior_angle_samples(tmp_path):
    sources = gimbal_sources(tmp_path)
    geometry = load_gimbal_collision_geometry(tmp_path, sources, {})
    bound = geometry.swept_bounds(180)
    sampled_min_up = min(
        geometry.center_body(roll, pitch, 0)[2]
        for roll in (-0.7, -0.35, 0.0, 0.35, 0.7)
        for pitch in (-2.2, -1.7, -1.1, -0.5, 0.1, 0.7)
    )
    assert bound["center_min_up_m"] <= sampled_min_up
    assert sampled_min_up < 0.0


def test_gimbal_ground_contact_uses_joint_range_bottom_not_fixed_camera_geometry(tmp_path):
    sources = gimbal_sources(tmp_path)
    environment, generated_source = gimbal_contact_environment(tmp_path, sources)
    result = contact_api.audit_signed_ground_contact_geometry(
        artifact_root=tmp_path,
        environment=environment,
        gimbal_sources=sources,
        gimbal_generated_sdf_source=generated_source,
        support_collision_id=SUPPORT,
    )
    row = result.to_mapping()
    assert row["gimbal_joint_range_contact_checked"] is True
    assert row["source_derived_gimbal_collision_bottom_up_m"] == pytest.approx(0.06162374358608)
    assert row["source_derived_all_collision_bottom_up_m"] == pytest.approx(0.016)
    assert row["fixed_collision_bottom_up_m"] == pytest.approx(0.016)
    assert row["gimbal_may_contact_named_support"] is True
    assert row["gimbal_support_geometry_overlap_m"] == pytest.approx(0.01837625641392)
    assert row["gimbal_changes_lowest_collision_point"] is False
    assert row["signed_bottom_minus_support_up_m"] == pytest.approx(-0.064)
    assert "penetration_exceeds_predeclared_numerical_allowance" in row["refusal_reasons"]
    assert row["support_contains_conservative_footprint"] is True
    assert not any(row["claims"].values())


def test_gimbal_asset_hash_drift_and_loaded_sphere_drift_fail_closed(tmp_path):
    inputs = _envelope_inputs(tmp_path)
    source_path = tmp_path / inputs["gimbal_sources"]["gimbal_model.sdf"].path
    source_path.write_bytes(source_path.read_bytes() + b" ")
    with pytest.raises(AutonomyContractError, match="identity/bytes/SHA"):
        audit_x500_collision_envelope(**inputs)

    second_root = tmp_path / "loaded-drift"
    second_root.mkdir()
    inputs = _envelope_inputs(second_root)
    response = decode_generated_sdf_reply((second_root / "gimbal-reply.bin").read_bytes())
    document = ET.fromstring(response)
    vehicle = next(
        model
        for model in document.find("world").findall("model")
        if model.attrib.get("name") == "x500_gimbal_0"
    )
    sphere = vehicle.find("link[@name='camera_link']/collision/pose")
    values = [float(item) for item in sphere.text.split()]
    values[2] += 0.01
    sphere.text = " ".join(map(str, values))
    payload = b"data: " + json.dumps(ET.tostring(document, encoding="unicode")).encode("utf-8")
    reply_path = second_root / "gimbal-reply.bin"
    reply_path.write_bytes(payload)
    raw = reply_path.read_bytes()
    inputs["generated_sdf_source"] = EvidenceBinding(
        "gimbal-reply.bin", len(raw), hashlib.sha256(raw).hexdigest(), "CPU-fixture", None
    )
    with pytest.raises(AutonomyContractError, match="collision geometry/center differs"):
        audit_x500_collision_envelope(**inputs)


def test_fixed_x500_contract_cannot_be_mixed_or_replaced_by_gimbal_closure(tmp_path):
    inputs = _envelope_inputs(tmp_path)
    with pytest.raises(AutonomyContractError, match="cannot be mixed"):
        audit_x500_collision_envelope(
            **inputs,
            model_sources={},
            extraction_source=inputs["gimbal_sources"]["extraction.json"],
        )
