"""Bounded CPU source/geometry fixtures; no Gazebo or flight acceptance."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.autonomy import sim_airframe_envelope as api
from multidetect.autonomy.onboard_executed_trajectory import EvidenceBinding
from multidetect.autonomy.serialization import AutonomyContractError, canonical_json


def _binding(root, path, pointer=None):
    raw = (root / path).read_bytes()
    return EvidenceBinding(path, len(raw), hashlib.sha256(raw).hexdigest(), "CPU-fixture", pointer)


def _box(name, pose, size):
    return (
        f'<collision name="{name}"><pose>{pose}</pose><geometry><box>'
        f"<size>{size}</size></box></geometry><surface><contact><ode>"
        "<min_depth>0.001</min_depth><max_vel>0</max_vel></ode></contact></surface></collision>"
    )


def _fixture(tmp_path):
    base = ET.Element("model", name="x500_base")
    ET.SubElement(base, "pose").text = "0 0 .24 0 0 0"
    body = ET.SubElement(base, "link", name="base_link")
    for name, kind in (("imu_sensor", "imu"), ("navsat_sensor", "navsat")):
        sensor = ET.SubElement(body, "sensor", name=name, type=kind)
        ET.SubElement(sensor, "gz_frame_id").text = "base_link"
    for i, (pose, size) in enumerate(
        [
            ("0 0 .007 0 0 0", ".35355339059327373 .35355339059327373 .05"),
            ("0 -.098 -.123 -.35 0 0", ".015 .015 .21"),
            ("0 .098 -.123 .35 0 0", ".015 .015 .21"),
            ("0 -.132 -.2195 0 0 0", ".25 .015 .015"),
            ("0 .132 -.2195 0 0 0", ".25 .015 .015"),
        ]
    ):
        body.append(ET.fromstring(_box(f"base_link_collision_{i}", pose, size)))
    for i, (x, y) in enumerate(
        ((0.174, -0.174), (-0.174, 0.174), (0.174, 0.174), (-0.174, -0.174))
    ):
        rotor = ET.SubElement(base, "link", name=f"rotor_{i}")
        ET.SubElement(rotor, "pose").text = f"{x} {y} .06 0 0 0"
        rotor.append(
            ET.fromstring(
                _box(
                    f"rotor_{i}_collision",
                    "0 0 0 0 0 0",
                    ".2792307692307692 .016923076923076923 .0008461538461538462",
                )
            )
        )
        joint = ET.SubElement(base, "joint", name=f"rotor_{i}_joint", type="revolute")
        ET.SubElement(joint, "parent").text = "base_link"
        ET.SubElement(joint, "child").text = f"rotor_{i}"
        axis = ET.SubElement(joint, "axis")
        ET.SubElement(axis, "xyz").text = "0 0 1"
    x500 = ET.fromstring(
        '<model name="x500"><include merge="true"><uri>model://x500_base</uri></include></model>'
    )
    mono = ET.fromstring("""<model name="x500_mono_cam">
    <include merge="true"><uri>model://x500</uri></include>
    <include merge="true"><uri>model://mono_cam</uri>
    <pose relative_to="base_link">.18 0 -.12 0 .18 0</pose></include>
    <plugin name="gz::sim::systems::OdometryPublisher"
    filename="gz-sim-odometry-publisher-system"><dimensions>3</dimensions>
    <odom_publish_frequency>50</odom_publish_frequency></plugin>
    </model>""")
    for name, element in (("x500_base", base), ("x500", x500), ("x500_mono_cam", mono)):
        root = ET.Element("sdf", version="1.9")
        root.append(element)
        (tmp_path / f"{name}.sdf").write_bytes(ET.tostring(root))
    world = ET.Element("world", name="default")
    physics = ET.SubElement(world, "physics", name="px4_ode", type="ode")
    ET.SubElement(physics, "max_step_size").text = ".004"
    pad = ET.SubElement(world, "model", name="multirotor_home_pad")
    ET.SubElement(pad, "pose").text = "0 0 .04 0 0 0"
    pad.append(
        ET.fromstring(
            """<link name="pad_link"><collision name="pad_collision"><geometry><cylinder>
        <radius>5</radius><length>.08</length></cylinder></geometry></collision></link>"""
        )
    )
    vehicle = ET.SubElement(world, "model", name="x500_mono_cam_0")
    ET.SubElement(vehicle, "pose").text = "0 0 .003 0 0 0"
    for name, attached, relative, pose in [
        ("_merged__x500__model__", "base_link", "__model__", "0 0 0 0 0 0"),
        ("_merged__x500_base__model__", "base_link", "_merged__x500__model__", "0 0 .24 0 0 0"),
        ("_merged__mono_cam__model__", "camera_link", "base_link", ".18 0 -.12 0 .18 0"),
    ]:
        frame = ET.SubElement(vehicle, "frame", name=name, attached_to=attached)
        ET.SubElement(frame, "pose", relative_to=relative).text = pose
    for link in base.findall("link"):
        loaded = ET.fromstring(ET.tostring(link))
        pose = loaded.find("pose")
        if pose is None:
            ET.SubElement(loaded, "pose").text = "0 0 .24 0 0 0"
        else:
            values = [float(v) for v in pose.text.split()]
            values[2] += 0.24
            pose.text = " ".join(map(str, values))
        vehicle.append(loaded)
    for name in ("camera_link", "depth_camera_link"):
        link = ET.SubElement(vehicle, "link", name=name)
        ET.SubElement(link, "pose").text = ".18 0 .12 0 .18 0"
    sdf = ET.Element("sdf", version="1.10")
    sdf.append(world)
    _write_reply(tmp_path, sdf)
    models = {
        name: _binding(tmp_path, f"{name}.sdf") for name in ("x500_mono_cam", "x500", "x500_base")
    }
    receipt = {
        "contract": "multidetect.teacher-Gazebo-navref-asset-extraction-v1",
        "status": "original-image-assets-recorded",
        "image": api.PINNED_IMAGE,
        "injected_native_used": False,
        "never_started_identity_verified": True,
        "owned_container_absent_after_cleanup": True,
        "error": None,
        "cleanup_error": None,
        "assets": {
            asset_name: {
                "image_path": image_path,
                "source": {
                    "path": str((tmp_path / models[name].path).resolve()),
                    "bytes": models[name].bytes,
                    "sha256": models[name].sha256,
                },
            }
            for name, (asset_name, image_path) in api._ASSETS.items()
        },
    }
    (tmp_path / "extraction.json").write_text(canonical_json(receipt), encoding="utf-8")
    return dict(
        artifact_root=tmp_path,
        model_sources=models,
        extraction_source=_binding(tmp_path, "extraction.json", ""),
        generated_sdf_source=_binding(tmp_path, "reply.bin"),
    )


def _write_reply(root, element):
    # JSON's ASCII escaping of this XML is also an exact supported StringMsg text field.
    (root / "reply.bin").write_bytes(
        b"data: " + json.dumps(ET.tostring(element, encoding="unicode")).encode()
    )


def _change_model(inputs, name, change):
    root = inputs["artifact_root"]
    path = root / inputs["model_sources"][name].path
    document = ET.fromstring(path.read_bytes())
    change(document[0])
    path.write_bytes(ET.tostring(document))
    binding = _binding(root, path.name)
    inputs["model_sources"][name] = binding
    if name in api._ASSETS:
        receipt_path = root / "extraction.json"
        receipt = json.loads(receipt_path.read_text())
        record = receipt["assets"][api._ASSETS[name][0]]["source"]
        record.update(bytes=binding.bytes, sha256=binding.sha256)
        receipt_path.write_text(canonical_json(receipt), encoding="utf-8")
        inputs["extraction_source"] = _binding(root, "extraction.json", "")


def _change_loaded(inputs, change):
    root = inputs["artifact_root"]
    document = api._xml(api.decode_generated_sdf_reply((root / "reply.bin").read_bytes()))
    vehicle = next(m for m in document[0].findall("model") if m.attrib["name"] == "x500_mono_cam_0")
    change(vehicle)
    _write_reply(root, document)
    inputs["generated_sdf_source"] = _binding(root, "reply.bin")


def test_recomputes_all_nine_boxes_and_spinning_rotors_without_safety_claims(tmp_path):
    inputs = _fixture(tmp_path)
    result = api.audit_x500_collision_envelope(**inputs)
    row = result.to_mapping()
    assert row["collision_count"] == 9
    assert row["model_to_body_offset_flu_m"] == [0, 0, 0.24]
    assert row["level_body_collision_bounds"] == pytest.approx(
        {
            "horizontal_radius_m": 0.3859448197013661,
            "bottom_offset_m": -0.2270001,
            "top_offset_m": 0.06042317692307692,
        }
    )
    assert row["envelope_bounds"] == pytest.approx(
        {
            "horizontal_radius_m": 0.3906460712965413,
            "bottom_offset_m": -0.3906460712965413,
            "top_offset_m": 0.3906460712965413,
        }
    )
    assert row["loaded_pose_diagnostic"]["rigid_body_collision_bottom_up_m"] == pytest.approx(0.016)
    assert row["loaded_pose_diagnostic"]["bottom_minus_pad_top_m"] == pytest.approx(-0.064)
    assert row["landing_support"]["status"] == "unknown"
    assert row["full_route_clearance_ready"] is False
    assert not any(row["claims"].values())
    assert row["attitude_domain"]["runtime_tilt_bound_verified"] is False
    api.validate_x500_collision_envelope(result, **inputs)
    row["envelope_bounds"]["horizontal_radius_m"] = 999
    assert result.envelope_bounds["horizontal_radius_m"] != 999


def test_reduced_tilt_is_explicit_design_only_and_not_level_hull_reuse(tmp_path):
    inputs = _fixture(tmp_path)
    tilted = api.audit_x500_collision_envelope(**inputs, maximum_tilt_deg=30).to_mapping()
    level = api.audit_x500_collision_envelope(**inputs, maximum_tilt_deg=0).to_mapping()
    assert (
        tilted["envelope_bounds"]["bottom_offset_m"] < level["envelope_bounds"]["bottom_offset_m"]
    )
    assert tilted["envelope_bounds"]["top_offset_m"] > level["envelope_bounds"]["top_offset_m"]
    assert (
        level["envelope_bounds"]["horizontal_radius_m"]
        > level["level_body_collision_bounds"]["horizontal_radius_m"]
    )
    assert tilted["attitude_domain"]["runtime_tilt_bound_verified"] is False


@pytest.mark.parametrize("tilt", [True, False, None, -1, 181, float("nan"), float("inf")])
def test_invalid_tilt_rejected(tmp_path, tilt):
    with pytest.raises((TypeError, ValueError)):
        api.audit_x500_collision_envelope(**_fixture(tmp_path), maximum_tilt_deg=tilt)


def test_analytic_orientation_bound_contains_rotating_rotor_and_rigid_corner_grid(tmp_path):
    inputs = _fixture(tmp_path)
    _, _, _, _, points = api._models(tmp_path, inputs["model_sources"], {})
    for angle in range(0, 360, 7):
        radius = math.hypot(0.2792307692307692 / 2, 0.016923076923076923 / 2)
        points.append(
            (
                0.174 + radius * math.cos(math.radians(angle)),
                -0.174 + radius * math.sin(math.radians(angle)),
                0.06042307692307692,
            )
        )
    for maximum_tilt in (0, 1, 30, 90, 180):
        bounds = api.audit_x500_collision_envelope(
            **inputs, maximum_tilt_deg=maximum_tilt
        ).envelope_bounds
        for tilt, azimuth, point in itertools.product(
            range(0, maximum_tilt + 1, max(1, maximum_tilt // 7)), range(0, 360, 37), points
        ):
            delta, yaw = math.radians(tilt), math.radians(azimuth)
            up = (
                math.sin(delta) * (math.cos(yaw) * point[0] + math.sin(yaw) * point[1])
                + math.cos(delta) * point[2]
            )
            assert bounds["bottom_offset_m"] <= up <= bounds["top_offset_m"]
            assert (
                math.sqrt(max(0, sum(v * v for v in point) - up * up))
                <= bounds["horizontal_radius_m"]
            )


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m.find("pose").__setattr__("text", "0 0 .25 0 0 0"),
        lambda m: m.append(ET.Element("include")),
        lambda m: m.append(ET.Element("frame", name="hidden")),
        lambda m: m.remove(m.findall("link")[-1]),
        lambda m: m.find("joint/axis/xyz").__setattr__("text", "1 0 0"),
        lambda m: m.find("joint/axis/xyz").set("expressed_in", "unknown"),
        lambda m: m.find("joint").set("type", "ball"),
        lambda m: m.find("joint/parent").__setattr__("text", "rotor_1"),
        lambda m: m.find("link/collision/geometry/box/size").__setattr__("text", "0 1 1"),
        lambda m: m.find("link/collision/geometry/box/size").__setattr__("text", "nan 1 1"),
        lambda m: m.find("link/collision/geometry").append(ET.Element("mesh")),
        lambda m: m.find("link/collision/pose").set("relative_to", "unknown"),
        lambda m: m.find("link").append(ET.fromstring(_box("extra", "0 0 0 0 0 0", "1 1 1"))),
    ],
)
def test_source_geometry_and_frame_unknowns_fail_closed(tmp_path, change):
    inputs = _fixture(tmp_path)
    _change_model(inputs, "x500_base", change)
    with pytest.raises(AutonomyContractError):
        api.audit_x500_collision_envelope(**inputs)


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m.append(ET.Element("link", name="extra")),
        lambda m: m.find("link").append(ET.fromstring(_box("extra", "0 0 0 0 0 0", "1 1 1"))),
        lambda m: m.find("link/collision/geometry/box/size").__setattr__("text", "1 1 1"),
        lambda m: m.find("link/pose").__setattr__("text", "0 0 .25 0 0 0"),
        lambda m: m.find("frame/pose").__setattr__("text", "1 0 0 0 0 0"),
        lambda m: m.find("frame").set("attached_to", "camera_link"),
        lambda m: m.append(ET.Element("frame", name="unknown")),
        lambda m: m.append(ET.Element("include")),
        lambda m: m.findall("link")[1].find("pose").__setattr__("text", ".174 -.174 .30 .1 0 0"),
        lambda m: m.findall("link")[-1].append(
            ET.fromstring(_box("camera", "0 0 0 0 0 0", "1 1 1"))
        ),
    ],
)
def test_loaded_geometry_merge_or_attachment_drift_rejected(tmp_path, change):
    inputs = _fixture(tmp_path)
    _change_loaded(inputs, change)
    with pytest.raises(AutonomyContractError):
        api.audit_x500_collision_envelope(**inputs)


def test_arbitrary_loaded_rotor_yaw_is_enclosed_by_source_sweep(tmp_path):
    inputs = _fixture(tmp_path)

    def change(m):
        m.findall("link")[1].find("pose").text = ".174 -.174 .30 0 0 1.72"

    _change_loaded(inputs, change)
    assert api.audit_x500_collision_envelope(**inputs).to_mapping()["collision_count"] == 9


@pytest.mark.parametrize(
    "key,value",
    [
        ("image", "unbound"),
        ("status", "failed"),
        ("injected_native_used", True),
        ("never_started_identity_verified", 1),
        ("owned_container_absent_after_cleanup", False),
        ("error", "failure"),
        ("cleanup_error", "failure"),
    ],
)
def test_extraction_failure_or_declared_identity_rejected(tmp_path, key, value):
    inputs = _fixture(tmp_path)
    path = tmp_path / "extraction.json"
    row = json.loads(path.read_text())
    row[key] = value
    path.write_text(canonical_json(row), encoding="utf-8")
    inputs["extraction_source"] = _binding(tmp_path, "extraction.json", "")
    with pytest.raises(AutonomyContractError):
        api.audit_x500_collision_envelope(**inputs)


def test_same_hash_elsewhere_is_not_retained_extracted_path(tmp_path):
    inputs = _fixture(tmp_path)
    original = tmp_path / inputs["model_sources"]["x500"].path
    (tmp_path / "other.sdf").write_bytes(original.read_bytes())
    inputs["model_sources"]["x500"] = _binding(tmp_path, "other.sdf")
    with pytest.raises(AutonomyContractError, match="differs from retained"):
        api.audit_x500_collision_envelope(**inputs)


def test_unknown_include_even_with_same_loaded_nine_collisions_rejected(tmp_path):
    inputs = _fixture(tmp_path)
    _change_model(
        inputs,
        "x500",
        lambda m: m.append(ET.fromstring("<include><uri>model://hidden</uri></include>")),
    )
    with pytest.raises(AutonomyContractError, match="unreviewed included"):
        api.audit_x500_collision_envelope(**inputs)


def test_final_source_drift_prevents_result(tmp_path, monkeypatch):
    inputs = _fixture(tmp_path)
    original = api._loaded

    def change(*args):
        value = original(*args)
        path = tmp_path / "x500_base.sdf"
        path.write_bytes(path.read_bytes() + b" ")
        return value

    monkeypatch.setattr(api, "_loaded", change)
    with pytest.raises(AutonomyContractError, match="identity/bytes/SHA"):
        api.audit_x500_collision_envelope(**inputs)


def test_atime_only_change_does_not_invalidate_content_identity(tmp_path, monkeypatch):
    inputs = _fixture(tmp_path)
    original = api._loaded

    def change(*args):
        value = original(*args)
        path = tmp_path / "x500_base.sdf"
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns + 1_000_000, stat.st_mtime_ns))
        return value

    monkeypatch.setattr(api, "_loaded", change)
    assert api.audit_x500_collision_envelope(**inputs).to_mapping()["collision_count"] == 9


def test_max_read_cap_whole_file_and_exact_model_keys(tmp_path):
    inputs = _fixture(tmp_path)
    inputs["generated_sdf_source"] = replace(
        inputs["generated_sdf_source"], bytes=api.MAXIMUM_SOURCE_BYTES + 1
    )
    with pytest.raises(AutonomyContractError, match="bounded read"):
        api.audit_x500_collision_envelope(**inputs)
    inputs = _fixture(tmp_path)
    inputs["extraction_source"] = replace(inputs["extraction_source"], json_pointer=None)
    with pytest.raises(AutonomyContractError, match="explicit whole"):
        api.audit_x500_collision_envelope(**inputs)
    inputs = _fixture(tmp_path)
    inputs["model_sources"].pop("x500")
    with pytest.raises(AutonomyContractError, match="exact whole-file"):
        api.audit_x500_collision_envelope(**inputs)


def test_stored_claim_cannot_override_source_recomputation(tmp_path):
    inputs = _fixture(tmp_path)
    good = api.audit_x500_collision_envelope(**inputs)
    row = good.to_mapping()
    row["envelope_bounds"]["horizontal_radius_m"] = 0.1
    forged = api.SourceDerivedSimAirframeEnvelopeAudit(canonical_json(row))
    with pytest.raises(AutonomyContractError, match="differs from recomputed"):
        api.validate_x500_collision_envelope(forged, **inputs)


def test_stored_boolean_cannot_be_replaced_by_equal_numeric_zero(tmp_path):
    inputs = _fixture(tmp_path)
    row = api.audit_x500_collision_envelope(**inputs).to_mapping()
    row["claims"]["mission_approved"] = 0
    forged = api.SourceDerivedSimAirframeEnvelopeAudit(canonical_json(row))
    with pytest.raises(AutonomyContractError, match="differs from recomputed"):
        api.validate_x500_collision_envelope(forged, **inputs)


def test_same_model_pose_z_is_not_body_z_and_tilt_rotates_lever_arm(tmp_path):
    inputs = _fixture(tmp_path)
    _change_loaded(
        inputs, lambda m: m.find("pose").__setattr__("text", "1 2 3 0 1.5707963267948966 0")
    )
    result = api.audit_x500_collision_envelope(**inputs).to_mapping()
    assert result["loaded_pose_diagnostic"]["body_reference_enu_m"] == pytest.approx([1.24, 2, 3])
    assert result["landing_support"]["status"] == "unknown"


def test_module_import_is_stdlib_only_in_fresh_process():
    root = Path(__file__).resolve().parents[1]
    code = (
        f"import sys;sys.path.insert(0,{str(root / 'src')!r});"
        "import multidetect.autonomy.sim_airframe_envelope;"
        "assert not (set(sys.modules)&{'torch','transformers','PIL','numpy','pymavlink'})"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code], capture_output=True, timeout=15
    )
    assert result.returncode == 0, result.stderr.decode()
