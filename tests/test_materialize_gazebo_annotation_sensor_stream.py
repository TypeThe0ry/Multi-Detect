from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import pytest

from scripts import materialize_gazebo_annotation_sensor_stream as extractor
from scripts.gazebo_depth_wire import build_depth_wire_record
from scripts.gazebo_sensor_stream import WIRE_HEADER_BYTES, build_wire_frame, unpack_header


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _record(sequence: int, stamp: int, contract: dict) -> bytes:
    rgb = build_wire_frame(
        sequence=sequence,
        sensor_stamp_ns=stamp,
        width=2,
        height=1,
        camera_info_stamp_ns=stamp,
        navsat_stamp_ns=stamp,
        odometry_stamp_ns=stamp - 1,
        latitude_deg=1.35,
        longitude_deg=103.81,
        altitude_m=22.0,
        position_east_m=1.0,
        position_north_m=2.0,
        position_up_m=3.0,
        orientation_x=0.0,
        orientation_y=0.0,
        orientation_z=0.0,
        orientation_w=1.0,
        velocity_forward_mps=0.0,
        velocity_left_mps=0.0,
        velocity_up_mps=0.0,
        attitude_covariance_enu_rad2=(0.0,) * 9,
        bgr_payload=bytes((1, 2, 3, 4, 5, sequence)),
    )
    header = unpack_header(rgb[:WIRE_HEADER_BYTES])
    depth = build_depth_wire_record(
        metadata={
            **contract,
            "sequence": sequence,
            "rgb_sensor_stamp_ns": stamp,
            "rgb_record_sha256": header.sha256.hex(),
            "source_stamp_ns": stamp,
            "stride_bytes": 8,
        },
        payload=struct.pack("<ff", 2.0, 3.0),
    )
    return rgb + depth


@pytest.fixture
def capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Small synthetic wire fixture; never evidence of a running Gazebo instance."""
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setattr(extractor, "ROOT", repository)
    base = repository / "base.sdf"
    base.write_text(
        '<sdf version="1.9"><world name="default"><include>'
        "<uri>model://neutral_target</uri></include></world></sdf>",
        encoding="utf-8",
    )
    world = tmp_path / "prepared-world.sdf"
    world.write_text(
        base.read_text(encoding="utf-8").replace(
            "model://neutral_target", "model://multidetect_annotation_target"
        ),
        encoding="utf-8",
    )
    mainline = repository / "mainline.json"
    _json(mainline, {"fixture_only": True})
    boundary = {
        "runtime_consumes_annotation_topics": False,
        "truth_injected_into_policy": False,
        "training_requires_independent_review": True,
        "policy_input_contract": "rgb_depth_camera_info_pose_telemetry_only",
    }
    wrappers = {}
    assets = []
    resources = tmp_path / "resources"
    for kind, name in (
        ("vehicle_wrapper", "multidetect_annotation_x500"),
        ("target_wrapper", "multidetect_annotation_target"),
    ):
        source = repository / name / "model.sdf"
        source.parent.mkdir()
        source.write_text(f'<sdf version="1.9"><model name="{name}"/></sdf>')
        copy = resources / name / "model.sdf"
        copy.parent.mkdir(parents=True)
        copy.write_bytes(source.read_bytes())
        wrappers[kind] = {
            "path": source.relative_to(repository).as_posix(),
            "sha256": _sha(source),
            "annotation_uri": f"model://{name}",
            "base_uri": "model://neutral_target" if kind == "target_wrapper" else "model://x500",
        }
        assets.append(
            {
                "path": copy.relative_to(resources).as_posix(),
                "source_path": source.as_posix(),
                "source_sha256": _sha(source),
                "output_sha256": _sha(copy),
                "bytes": copy.stat().st_size,
            }
        )
    config = repository / "annotation-config.json"
    topics = {"bounding_boxes_3d": "annotation/boxes", "instance_segmentation": "annotation/labels"}
    _json(
        config,
        {
            "schema": "multidetect.gazebo-3d-annotation-capture",
            "contract": "multidetect.gazebo-3d-annotation-capture-v1",
            "purpose": "offline_3d_annotation_only",
            "runtime_boundary": boundary,
            "mainline_manifest": {"path": "mainline.json", "sha256": _sha(mainline)},
            "base_world": {"path": "base.sdf", "sha256": _sha(base)},
            **wrappers,
            "sensors": {
                "bounding_box": {"topic": topics["bounding_boxes_3d"]},
                "segmentation": {"topic": topics["instance_segmentation"]},
            },
        },
    )
    binding = tmp_path / "binding.json"
    _json(
        binding,
        {
            "schema": "multidetect.gazebo-3d-annotation-materialization",
            "schema_version": 1,
            "contract": "multidetect.gazebo-3d-annotation-materialization-v1",
            "status": "prepared_not_executed",
            "external_gazebo_executed": False,
            "training_ready": False,
            "review_required": True,
            "runtime_boundary": boundary,
            "source_world": {"path": base.as_posix(), "sha256": _sha(base)},
            "output_world": {"path": world.as_posix(), "sha256": _sha(world)},
            "resource_root": {
                "path": resources.as_posix(),
                "files": assets,
                "file_count": len(assets),
                "mount_read_only": True,
            },
            "vehicle_model_uri": wrappers["vehicle_wrapper"]["annotation_uri"],
            "target_replacement": {
                "count": 1,
                "from": "model://neutral_target",
                "to": wrappers["target_wrapper"]["annotation_uri"],
            },
            "annotation_topics": topics,
        },
    )
    prefix = "/world/default/model/multidetect_annotation_x500_0"
    contract = {
        "calibration_id": "depth-fixture",
        "calibration_sha256": "b" * 64,
        "source_topic": f"{prefix}/link/depth_camera_link/sensor/depth_camera/depth_image",
        "width": 2,
        "height": 1,
        "fx_px": 1.0,
        "fy_px": 1.0,
        "cx_px": 1.0,
        "cy_px": 0.5,
        "minimum_depth_m": 0.1,
        "maximum_depth_m": 10.0,
        "encoding": "FLOAT32_M",
        "distance_semantics": "optical_axis_z_m",
        "coordinate_space": "rgb_pixel_aligned",
        "registration_mode": "registered_to_rgb",
    }
    contract_path = tmp_path / "depth-contract.json"
    _json(contract_path, contract)
    calibration = tmp_path / "camera.json"
    _json(
        calibration,
        {
            "width_px": 2,
            "height_px": 1,
            "fx_px": 1.0,
            "fy_px": 1.0,
            "cx_px": 1.0,
            "cy_px": 0.5,
            "distortion": [0.0] * 5,
        },
    )
    status = "validated_k_p_rectification_plumb_bob_distortion"
    started = {
        "event": "gazebo_sensor_stream_started",
        "wire_magic": "MDGZSIM3",
        "wire_version": 3,
        "depth_enabled": True,
        "requested_frames": 2,
        "depth_contract": contract,
        "image_topic": f"{prefix}/link/camera_link/sensor/camera/image",
        "camera_info_topic": f"{prefix}/link/camera_link/sensor/camera/camera_info",
        "depth_topic": contract["source_topic"],
        "depth_camera_info_topic": (
            f"{prefix}/link/depth_camera_link/sensor/depth_camera/camera_info"
        ),
        "navsat_topic": f"{prefix}/link/base_link/sensor/navsat_sensor/navsat",
        "odometry_topic": "/model/multidetect_annotation_x500_0/odometry_with_covariance",
    }
    finished = {
        "event": "gazebo_sensor_stream_finished",
        "gate_passed": True,
        "error": None,
        "requested_frames": 2,
        "accepted_frames": 2,
        "emitted_frames": 2,
        "depth_enabled": True,
        "depth_contract": contract,
        "camera_info_intrinsics_semantic_validation": status,
        "camera_info_semantics": {
            "width": 2,
            "height": 1,
            "distortion_model": "PLUMB_BOB",
            "status": status,
            "k": [1.0, 0.0, 1.0, 0.0, 1.0, 0.5, 0.0, 0.0, 1.0],
            "p": [1.0, 0.0, 1.0, 0.0, 0.0, 1.0, 0.5, 0.0, 0.0, 0.0, 1.0, 0.0],
            "r": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
            "d": [0.0] * 5,
        },
    }
    evidence = tmp_path / "sensor.stderr.log"
    evidence.write_text(json.dumps(started) + "\n" + json.dumps(finished) + "\n", encoding="utf-8")
    stream = tmp_path / "sensor.bin"
    stream.write_bytes(
        _record(0, 167_508_000_000, contract) + _record(1, 167_583_000_000, contract)
    )
    return {
        "stream_path": stream,
        "output_root": tmp_path / "extraction",
        "depth_contract_path": contract_path,
        "annotation_binding_path": binding,
        "annotation_config_path": config,
        "camera_evidence_path": evidence,
        "camera_calibration_path": calibration,
        "frame_count": 2,
    }


def test_extracts_only_offline_sensor_time_and_hash_bound_inputs(capture: dict) -> None:
    source_hash = _sha(capture["stream_path"])
    receipt = extractor.materialize(**capture)
    document = _read(capture["output_root"] / "staging-manifest.json")
    assert document["schema"] == "multidetect.gazebo-annotation-sensor-staging"
    assert document["training_ready"] is False
    assert document["capture_authenticated"] is False
    assert document["production_mainline_asset_binding"] == "not_claimed"
    assert document["frames"][0]["sensor_time_s"] == 0.0
    assert document["frames"][1]["sensor_time_s"] == 0.075
    assert {row["captured_at_clock"] for row in document["frames"]} == {"gazebo_sensor"}
    assert "captured_at_s" not in document["frames"][0]
    assert document["annotation_binding"]["mounted_assets_verified"] is False
    assert document["camera_evidence"]["observed_camera_info"]["k"][0] == 1.0
    assert receipt["camera_evidence_sha256"] == _sha(capture["camera_evidence_path"])
    assert receipt["camera_calibration_sha256"] == _sha(capture["camera_calibration_path"])
    assert receipt["annotation_config_sha256"] == _sha(capture["annotation_config_path"])
    assert receipt["annotation_binding_sha256"] == _sha(capture["annotation_binding_path"])
    assert _sha(capture["stream_path"]) == source_hash
    assert not list(capture["output_root"].rglob("*frame-set*"))
    assert not list(capture["output_root"].rglob("*depth-companion*"))
    for row in document["frames"]:
        rgb = capture["output_root"] / row["rgb_payload_path"]
        depth = capture["output_root"] / row["depth_payload_path"]
        assert _sha(rgb) == row["rgb_payload_sha256"]
        assert _sha(depth) == row["depth_payload_sha256"]
        assert (
            hashlib.sha256(
                bytes.fromhex(row["rgb_unsigned_header_hex"]) + rgb.read_bytes()
            ).hexdigest()
            == row["rgb_record_sha256"]
        )


@pytest.mark.parametrize(
    "field",
    [
        "camera_evidence_path",
        "camera_calibration_path",
        "annotation_binding_path",
        "annotation_config_path",
    ],
)
def test_missing_evidence_fails_before_output(capture: dict, field: str) -> None:
    capture[field] = capture[field].with_name("absent")
    with pytest.raises(ValueError, match="missing"):
        extractor.materialize(**capture)
    assert not capture["output_root"].exists()


@pytest.mark.parametrize("mutation", ["fake_pass", "topic", "missing_camera", "not_passed"])
def test_camera_log_must_have_actual_matching_semantics(capture: dict, mutation: str) -> None:
    path = capture["camera_evidence_path"]
    started, finished = [json.loads(line) for line in path.read_text().splitlines()]
    if mutation == "fake_pass":
        finished["camera_info_semantics"]["k"][0] = 7.0
    elif mutation == "topic":
        started["camera_info_topic"] = "/world/default/model/x500_0/camera_info"
    elif mutation == "missing_camera":
        finished.pop("camera_info_semantics")
    else:
        finished["gate_passed"] = False
    path.write_text(json.dumps(started) + "\n" + json.dumps(finished) + "\n")
    with pytest.raises(ValueError, match="CameraInfo|camera evidence"):
        extractor.materialize(**capture)
    assert not capture["output_root"].exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "traversal",
        "duplicate",
        "extra_file",
        "asset_hash",
        "fake_execution",
        "truth_input",
        "world_change",
    ],
)
def test_annotation_assets_and_boundary_are_fail_closed(capture: dict, mutation: str) -> None:
    path = capture["annotation_binding_path"]
    document = _read(path)
    resources = Path(document["resource_root"]["path"])
    if mutation == "traversal":
        document["resource_root"]["files"][0]["path"] = "../outside.sdf"
    elif mutation == "duplicate":
        document["resource_root"]["files"].append(document["resource_root"]["files"][0])
    elif mutation == "extra_file":
        (resources / "not-in-receipt.txt").write_text("extra")
    elif mutation == "asset_hash":
        (resources / document["resource_root"]["files"][0]["path"]).write_text("changed")
    elif mutation == "fake_execution":
        document["external_gazebo_executed"] = True
    elif mutation == "truth_input":
        document["runtime_boundary"]["truth_injected_into_policy"] = True
    else:
        world = Path(document["output_world"]["path"])
        world.write_text(world.read_text().replace('name="default"', 'name="changed"'))
        document["output_world"]["sha256"] = _sha(world)
    _json(path, document)
    with pytest.raises(ValueError):
        extractor.materialize(**capture)
    assert not capture["output_root"].exists()


@pytest.mark.parametrize("mutation", ["truncated", "trailing", "duplicate_rgb", "rgb_tamper"])
def test_bad_binary_never_seals_staging(capture: dict, mutation: str) -> None:
    path = capture["stream_path"]
    raw = path.read_bytes()
    if mutation == "truncated":
        raw = raw[:-1]
    elif mutation == "trailing":
        raw += b"extra"
    elif mutation == "duplicate_rgb":
        contract = _read(capture["depth_contract_path"])
        raw = _record(0, 167_508_000_000, contract) + _record(0, 167_583_000_000, contract)
    else:
        raw = raw[:WIRE_HEADER_BYTES] + b"\xff" + raw[WIRE_HEADER_BYTES + 1 :]
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        extractor.materialize(**capture)
    assert not (capture["output_root"] / "staging-manifest.json").exists()
    assert not (capture["output_root"] / "staging-receipt.json").exists()


def test_input_change_during_extraction_is_not_sealed(capture: dict, monkeypatch) -> None:
    original = extractor._write_exclusive
    changed = False

    def mutate_after_write(path: Path, payload: bytes) -> None:
        nonlocal changed
        original(path, payload)
        if not changed:
            capture["camera_calibration_path"].write_text("{}")
            changed = True

    monkeypatch.setattr(extractor, "_write_exclusive", mutate_after_write)
    with pytest.raises(ValueError, match="changed during"):
        extractor.materialize(**capture)
    assert not (capture["output_root"] / "staging-manifest.json").exists()


def test_refuses_overwrite_without_changing_existing_extraction(capture: dict) -> None:
    extractor.materialize(**capture)
    receipt = capture["output_root"] / "staging-receipt.json"
    prior_hash = _sha(receipt)
    with pytest.raises(FileExistsError):
        extractor.materialize(**capture)
    assert _sha(receipt) == prior_hash


def test_duplicate_json_keys_fail_closed(capture: dict) -> None:
    capture["annotation_config_path"].write_text('{"schema": "x", "schema": "y"}')
    with pytest.raises(ValueError, match="duplicate JSON key"):
        extractor.materialize(**capture)
    assert not capture["output_root"].exists()


def test_offline_staging_is_rejected_by_training_dataset_builder(capture: dict) -> None:
    from scripts.build_onboard_rendered_3d_dataset import build_dataset

    extractor.materialize(**capture)
    with pytest.raises(ValueError, match="unsupported RGB manifest"):
        build_dataset(
            [capture["output_root"] / "staging-manifest.json"],
            depth_manifests=[],
            detector_path=None,
            detector_confidence=0.25,
            max_frames_per_manifest=None,
        )
