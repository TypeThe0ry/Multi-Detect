#!/usr/bin/env python3
"""Extract a Gazebo annotation capture into offline-only sensor staging.

The source stream contains Gazebo sensor time, not a host monotonic receipt,
and was captured with annotation-only wrapper assets.  This command therefore
preserves raw pixels, depth, wire hashes, simulation timestamps, telemetry and
the actual annotation binding without creating the production RGB/depth
manifests consumed by the training pipeline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

if __package__ in {None, ""}:
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    source = root / "src"
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from multidetect.backends.gazebo_sim import reconstruct_gazebo_sim_wire_frame  # noqa: E402
from scripts.gazebo_depth_wire import read_depth_wire_frame  # noqa: E402
from scripts.gazebo_sensor_stream import (  # noqa: E402
    WireOrderValidator,
    _parse_expected_camera_calibration,
    pack_header,
    read_wire_frame,
    validate_camera_info_semantics,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STREAM = (
    ROOT / "artifacts/evaluation/onboard-3d-annotation-urban-20261007-r1/sensor-stream.bin"
)
DEFAULT_OUTPUT = (
    ROOT / "artifacts/evaluation/onboard-3d-annotation-urban-20261007-r1/materialized-r6-staging"
)
DEFAULT_DEPTH_CONTRACT = (
    ROOT / "artifacts/evaluation/onboard-3d-annotation-urban-20261007-r1-depth-contract.json"
)
DEFAULT_BINDING = ROOT / "artifacts/annotation-capture/binding-r2.json"
DEFAULT_ANNOTATION_CONFIG = ROOT / "configs/scenarios/gazebo-3d-annotation-capture-v1.json"
DEFAULT_CAMERA_EVIDENCE = DEFAULT_STREAM.with_name("sensor-stream.stderr.log")
DEFAULT_CAMERA_CALIBRATION = (
    ROOT / "artifacts/evaluation/onboard-3d-annotation-urban-20261007-r1-camera-calibration.json"
)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON value: {value}")


def _parse_json(raw: str) -> object:
    return json.loads(raw, object_pairs_hook=_strict_object, parse_constant=_reject_nonfinite)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_exclusive(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()


def _write_json_exclusive(path: Path, document: object) -> None:
    _write_exclusive(
        path,
        (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def _load_json_object(path: Path, *, name: str) -> dict[str, object]:
    document = _parse_json(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{name} must be a JSON object")
    return document


def _load_depth_contract(path: Path) -> dict[str, object]:
    document = _load_json_object(path, name="depth contract")
    required = {
        "calibration_id",
        "calibration_sha256",
        "coordinate_space",
        "cx_px",
        "cy_px",
        "distance_semantics",
        "encoding",
        "height",
        "maximum_depth_m",
        "minimum_depth_m",
        "registration_mode",
        "source_topic",
        "width",
        "fx_px",
        "fy_px",
    }
    missing = sorted(required.difference(document))
    if missing:
        raise ValueError(f"depth contract missing keys: {', '.join(missing)}")
    return {key: document[key] for key in sorted(required)}


def _require_sha256(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("evidence hash must be a lowercase SHA-256")
    return value


def _file_binding(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"evidence file is missing or is a symlink: {path}")
    return {"path": path.as_posix(), "sha256": _sha256(path), "bytes": path.stat().st_size}


def _configured_file(entry: object) -> dict[str, object]:
    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
        raise ValueError("annotation config file binding is missing")
    candidate = (ROOT / str(entry["path"])).expanduser()
    if candidate.is_symlink() or not candidate.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("annotation config file escaped the repository")
    file = _file_binding(candidate.resolve())
    if file["sha256"] != _require_sha256(entry.get("sha256")):
        raise ValueError("annotation config file hash mismatch")
    return file


def _boundary(document: dict[str, object]) -> dict[str, object]:
    value = document.get("runtime_boundary")
    expected = {
        "runtime_consumes_annotation_topics": False,
        "truth_injected_into_policy": False,
        "training_requires_independent_review": True,
    }
    if not isinstance(value, dict) or any(value.get(k) is not v for k, v in expected.items()):
        raise ValueError("annotation runtime truth boundary is invalid")
    if value.get("policy_input_contract") != "rgb_depth_camera_info_pose_telemetry_only":
        raise ValueError("annotation policy input contract is invalid")
    return value


def _xml_identity(element: ET.Element) -> object:
    return (
        element.tag,
        tuple(sorted(element.attrib.items())),
        (element.text or "").strip(),
        tuple(_xml_identity(child) for child in element),
    )


def _verify_annotation_binding(path: Path, config_path: Path) -> dict[str, object]:
    binding = _load_json_object(path, name="annotation binding")
    config = _load_json_object(config_path, name="annotation config")
    if (
        binding.get("schema") != "multidetect.gazebo-3d-annotation-materialization"
        or binding.get("contract") != "multidetect.gazebo-3d-annotation-materialization-v1"
        or binding.get("schema_version") != 1
    ):
        raise ValueError("annotation binding schema is not the v1 materialization binding")
    if (
        config.get("schema") != "multidetect.gazebo-3d-annotation-capture"
        or config.get("contract") != "multidetect.gazebo-3d-annotation-capture-v1"
        or config.get("purpose") != "offline_3d_annotation_only"
    ):
        raise ValueError("annotation config is not capture-only v1")
    boundary = _boundary(binding)
    if _boundary(config) != boundary:
        raise ValueError("annotation config and materialization boundaries disagree")
    if (
        binding.get("status") != "prepared_not_executed"
        or binding.get("external_gazebo_executed") is not False
        or binding.get("training_ready") is not False
        or binding.get("review_required") is not True
    ):
        raise ValueError("preparation receipt cannot claim execution or training readiness")
    config_files = {
        name: _configured_file(config.get(name))
        for name in ("mainline_manifest", "base_world", "vehicle_wrapper", "target_wrapper")
    }
    vehicle = config["vehicle_wrapper"]
    target = config["target_wrapper"]
    if not isinstance(vehicle, dict) or not isinstance(target, dict):
        raise ValueError("annotation wrappers are missing")
    vehicle_uri = vehicle.get("annotation_uri")
    target_uri = target.get("annotation_uri")
    if (
        not isinstance(vehicle_uri, str)
        or not vehicle_uri.startswith("model://multidetect_annotation_")
        or not isinstance(target_uri, str)
        or not target_uri.startswith("model://multidetect_annotation_")
        or binding.get("vehicle_model_uri") != vehicle_uri
        or binding.get("target_replacement")
        != {"count": 1, "from": target.get("base_uri"), "to": target_uri}
    ):
        raise ValueError("annotation wrapper model binding mismatch")
    source_world = binding.get("source_world")
    if (
        not isinstance(source_world, dict)
        or source_world.get("sha256") != config_files["base_world"]["sha256"]
        or Path(str(source_world.get("path"))).resolve()
        != Path(str(config_files["base_world"]["path"]))
    ):
        raise ValueError("annotation source world does not match config")
    output_world = binding.get("output_world")
    resource_root = binding.get("resource_root")
    if not isinstance(output_world, dict) or not isinstance(resource_root, dict):
        raise ValueError("annotation binding lacks output_world/resource_root")
    output_path = Path(str(output_world.get("path"))).expanduser()
    world_file = _file_binding(output_path)
    if world_file["sha256"] != _require_sha256(output_world.get("sha256")):
        raise ValueError("annotation output world is absent or hash-mismatched")
    base_xml = ET.parse(str(config_files["base_world"]["path"])).getroot()
    output_xml = ET.parse(output_path).getroot()
    replaced = [item for item in output_xml.iter("uri") if item.text == target_uri]
    if len(replaced) != 1:
        raise ValueError("annotation world target replacement is incomplete")
    replaced[0].text = str(target["base_uri"])
    if _xml_identity(output_xml) != _xml_identity(base_xml):
        raise ValueError("annotation world differs from the configured target-only replacement")
    resource_path = Path(str(resource_root.get("path"))).expanduser()
    files = resource_root.get("files")
    if not resource_path.is_dir() or not isinstance(files, list) or not files:
        raise ValueError("annotation resource root is absent or incomplete")
    if resource_path.is_symlink() or resource_root.get("mount_read_only") is not True:
        raise ValueError("annotation resource root must be regular and read-only bound")
    resource_path = resource_path.resolve()
    verified_files: dict[str, dict[str, object]] = {}
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("annotation resource file entry is invalid")
        raw_relative = entry.get("path")
        if (
            not isinstance(raw_relative, str)
            or "\\" in raw_relative
            or ":" in raw_relative
            or PurePosixPath(raw_relative).is_absolute()
            or any(part in {"", ".", ".."} for part in raw_relative.split("/"))
            or raw_relative in verified_files
        ):
            raise ValueError("annotation resource path is unsafe or duplicated")
        candidate = resource_path / raw_relative
        if not candidate.resolve().is_relative_to(resource_path):
            raise ValueError("annotation resource path escaped its root")
        if any(parent.is_symlink() for parent in candidate.parents if parent != resource_path):
            raise ValueError("annotation resource path contains a symlink")
        file = _file_binding(candidate)
        source_path_value = entry.get("source_path")
        if not isinstance(source_path_value, str):
            raise ValueError("annotation resource source path is missing")
        source_path = Path(source_path_value).expanduser()
        if not source_path.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError("annotation resource source path escaped the repository")
        source_file = _file_binding(source_path)
        if (
            file["sha256"] != _require_sha256(entry.get("output_sha256"))
            or file["sha256"] != _require_sha256(entry.get("source_sha256"))
            or file["sha256"] != source_file["sha256"]
            or isinstance(entry.get("bytes"), bool)
            or entry.get("bytes") != file["bytes"]
        ):
            raise ValueError(
                f"annotation resource file is absent or hash-mismatched: {raw_relative}"
            )
        verified_files[raw_relative] = {
            "path": raw_relative,
            "sha256": file["sha256"],
            "bytes": file["bytes"],
        }
    resource_entries = tuple(resource_path.rglob("*"))
    if any(
        item.is_symlink() or getattr(item, "is_junction", lambda: False)()
        for item in resource_entries
    ):
        raise ValueError("annotation resource root contains a symlink or junction")
    observed_files = {
        item.relative_to(resource_path).as_posix()
        for item in resource_entries
        if item.is_file()
    }
    if observed_files != set(verified_files) or resource_root.get("file_count") != len(files):
        raise ValueError("annotation resource root has missing, extra, or uncounted files")
    for name, uri in (("vehicle_wrapper", vehicle_uri), ("target_wrapper", target_uri)):
        resource = verified_files.get(f"{uri.removeprefix('model://')}/model.sdf")
        if resource is None or resource["sha256"] != config_files[name]["sha256"]:
            raise ValueError("annotation wrapper resource hash does not match config")
    sensors = config.get("sensors")
    if not isinstance(sensors, dict):
        raise ValueError("annotation sensor config is missing")
    expected_topics = {
        "bounding_boxes_3d": sensors["bounding_box"]["topic"],
        "instance_segmentation": sensors["segmentation"]["topic"],
    }
    if binding.get("annotation_topics") != expected_topics:
        raise ValueError("annotation topic binding differs from config")
    asset_rows = [verified_files[name] for name in sorted(verified_files)]
    asset_hash = _sha256_bytes(
        json.dumps(asset_rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return {
        "binding_path": path.as_posix(),
        "binding_sha256": _sha256(path),
        "binding_status": "prepared_not_executed",
        "external_gazebo_executed": False,
        "mounted_assets_verified": False,
        "asset_verification_scope": "provided_preparation_files_not_runtime_mount_attestation",
        "output_world": world_file,
        "source_world": config_files["base_world"],
        "config_files": config_files,
        "resource_root": {
            "path": resource_path.as_posix(),
            "file_count": len(asset_rows),
            "files": asset_rows,
            "assets_sha256": asset_hash,
            "hash_algorithm": "sha256(sorted_compact_json_path_sha256_bytes_rows)",
        },
        "vehicle_model_uri": vehicle_uri,
        "target_replacement": binding["target_replacement"],
        "annotation_topics": expected_topics,
        "runtime_boundary": boundary,
        "training_ready": False,
    }


def _verify_camera_evidence(
    *,
    path: Path,
    calibration_path: Path,
    binding: dict[str, object],
    depth_contract: dict[str, object],
    frame_count: int,
) -> dict[str, object]:
    evidence_file = _file_binding(path)
    calibration_file = _file_binding(calibration_path)
    expected = _parse_expected_camera_calibration(calibration_path.read_text(encoding="utf-8"))
    if expected is None:
        raise ValueError("expected camera calibration is missing")
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("camera evidence log exceeds the offline extraction bound")
    events = [_parse_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if len(events) != 2 or any(not isinstance(item, dict) for item in events):
        raise ValueError("camera evidence requires one started and one finished event")
    started, finished = events
    if (
        started.get("event") != "gazebo_sensor_stream_started"
        or finished.get("event") != "gazebo_sensor_stream_finished"
        or finished.get("gate_passed") is not True
        or finished.get("error") is not None
        or any(
            finished.get(k) != frame_count
            for k in ("requested_frames", "accepted_frames", "emitted_frames")
        )
        or started.get("requested_frames") != frame_count
        or started.get("wire_magic") != "MDGZSIM3"
        or started.get("wire_version") != 3
        or started.get("depth_enabled") is not True
        or finished.get("depth_enabled") is not True
    ):
        raise ValueError("camera evidence capture gate did not pass for the requested stream")
    source_topic = str(depth_contract["source_topic"])
    model_name = str(binding["vehicle_model_uri"]).removeprefix("model://")
    match = re.fullmatch(
        rf"(/world/[^/]+/model/{re.escape(model_name)}_[0-9]+)"
        r"/link/depth_camera_link/sensor/depth_camera/depth_image",
        source_topic,
    )
    if match is None:
        raise ValueError("depth source is not the configured annotation vehicle")
    prefix = match[1]
    topics = {
        "image_topic": f"{prefix}/link/camera_link/sensor/camera/image",
        "camera_info_topic": f"{prefix}/link/camera_link/sensor/camera/camera_info",
        "depth_topic": source_topic,
        "depth_camera_info_topic": (
            f"{prefix}/link/depth_camera_link/sensor/depth_camera/camera_info"
        ),
        "navsat_topic": f"{prefix}/link/base_link/sensor/navsat_sensor/navsat",
        "odometry_topic": f"/model/{prefix.split('/')[-1]}/odometry_with_covariance",
    }
    if any(started.get(key) != value for key, value in topics.items()):
        raise ValueError("camera evidence topic does not match annotation stream binding")
    for event in events:
        observed_contract = event.get("depth_contract")
        if not isinstance(observed_contract, dict) or any(
            observed_contract.get(key) != value for key, value in depth_contract.items()
        ):
            raise ValueError("camera evidence depth contract differs from source contract")
    semantics = finished.get("camera_info_semantics")
    if not isinstance(semantics, dict) or semantics.get("distortion_model") != "PLUMB_BOB":
        raise ValueError("observed CameraInfo semantics are missing or unsupported")
    message = SimpleNamespace(
        width=semantics.get("width"),
        height=semantics.get("height"),
        intrinsics=SimpleNamespace(k=semantics.get("k", [])),
        projection=SimpleNamespace(p=semantics.get("p", [])),
        rectification_matrix=semantics.get("r", []),
        distortion=SimpleNamespace(k=semantics.get("d", []), model=0),
    )
    try:
        validated = validate_camera_info_semantics(message, **expected)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"observed CameraInfo failed semantic validation: {exc}") from exc
    if (
        semantics.get("status") != validated["status"]
        or finished.get("camera_info_intrinsics_semantic_validation") != validated["status"]
    ):
        raise ValueError("camera evidence validation status is inconsistent")
    for contract_field, calibration_field in (
        ("width", "expected_width"),
        ("height", "expected_height"),
        ("fx_px", "expected_fx_px"),
        ("fy_px", "expected_fy_px"),
        ("cx_px", "expected_cx_px"),
        ("cy_px", "expected_cy_px"),
    ):
        if depth_contract[contract_field] != expected[calibration_field]:
            raise ValueError("depth contract is inconsistent with expected RGB calibration")
    return {
        "evidence_file": evidence_file,
        "expected_calibration_file": calibration_file,
        "observed_camera_info": validated,
        "topics": topics,
        "camera_info_semantic_validation": validated["status"],
        "capture_clock_observed": False,
        "evidence_scope": "supplied_sensor_log_not_authenticated_capture_receipt",
        "stream_sha256_present_in_log": False,
        "runtime_mount_attestation_present": False,
    }


def materialize(
    *,
    stream_path: Path,
    output_root: Path,
    depth_contract_path: Path,
    annotation_binding_path: Path,
    annotation_config_path: Path,
    camera_evidence_path: Path,
    camera_calibration_path: Path,
    frame_count: int,
) -> dict[str, object]:
    if isinstance(frame_count, bool) or not isinstance(frame_count, int) or frame_count <= 0:
        raise ValueError("frame_count must be a positive integer")
    input_paths = [
        path.expanduser()
        for path in (
            stream_path,
            depth_contract_path,
            annotation_binding_path,
            annotation_config_path,
            camera_evidence_path,
            camera_calibration_path,
        )
    ]
    input_files = [_file_binding(path) for path in input_paths]
    (
        stream_path,
        depth_contract_path,
        annotation_binding_path,
        annotation_config_path,
        camera_evidence_path,
        camera_calibration_path,
    ) = (path.resolve(strict=True) for path in input_paths)
    output_root = output_root.expanduser().resolve()
    rgb_root = output_root / "rgb"
    depth_root = output_root / "depth"
    depth_contract = _load_depth_contract(depth_contract_path)
    binding = _verify_annotation_binding(annotation_binding_path, annotation_config_path)
    annotation_config = _file_binding(annotation_config_path)
    camera_evidence = _verify_camera_evidence(
        path=camera_evidence_path,
        calibration_path=camera_calibration_path,
        binding=binding,
        depth_contract=depth_contract,
        frame_count=frame_count,
    )
    output_root.mkdir(parents=True, exist_ok=False)
    rgb_root.mkdir()
    depth_root.mkdir()
    source_topic = str(depth_contract["source_topic"])
    calibration_id = str(depth_contract["calibration_id"])
    calibration_sha256 = str(depth_contract["calibration_sha256"])
    order = WireOrderValidator()
    frames: list[dict[str, object]] = []
    first_sensor_stamp_ns: int | None = None
    with stream_path.open("rb") as stream:
        previous_stamp: int | None = None
        for expected_sequence in range(frame_count):
            header, payload = read_wire_frame(stream, order=order)
            if header.sequence != expected_sequence:
                raise ValueError(
                    f"unexpected RGB sequence: {header.sequence} != {expected_sequence}"
                )
            unsigned_header = pack_header(replace(header, sha256=b"\x00" * 32))
            wire = reconstruct_gazebo_sim_wire_frame(
                unsigned_header_hex=unsigned_header.hex(),
                payload_bgr24=payload,
                expected_sequence=header.sequence,
                expected_width=header.width,
                expected_height=header.height,
                previous_sensor_stamp_ns=previous_stamp,
            )
            previous_stamp = header.sensor_stamp_ns
            if first_sensor_stamp_ns is None:
                first_sensor_stamp_ns = header.sensor_stamp_ns
            depth_wire = read_depth_wire_frame(
                stream,
                expected_sequence=header.sequence,
                expected_rgb_sensor_stamp_ns=header.sensor_stamp_ns,
                expected_rgb_record_sha256=wire.record_sha256,
                expected_source_topic=source_topic,
                expected_calibration_id=calibration_id,
                expected_calibration_sha256=calibration_sha256,
            )
            if any(depth_wire.metadata.get(k) != v for k, v in depth_contract.items()):
                raise ValueError("depth wire metadata differs from the supplied capture contract")
            if (header.width, header.height) != (depth_contract["width"], depth_contract["height"]):
                raise ValueError("RGB/depth geometry does not match supplied CameraInfo evidence")
            rgb_name = f"{header.sequence:06d}.bgr24"
            depth_name = f"{header.sequence:06d}.f32"
            _write_exclusive(rgb_root / rgb_name, payload)
            _write_exclusive(depth_root / depth_name, depth_wire.payload)
            frames.append(
                {
                    "sequence": header.sequence,
                    "frame_id": f"gazebo-sim-{header.sequence:06d}",
                    "sensor_stamp_ns": header.sensor_stamp_ns,
                    "sensor_time_s": (header.sensor_stamp_ns - first_sensor_stamp_ns)
                    / 1_000_000_000.0,
                    "captured_at_clock": "gazebo_sensor",
                    "rgb_payload_path": f"rgb/{rgb_name}",
                    "rgb_payload_sha256": _sha256_bytes(payload),
                    "rgb_record_sha256": wire.record_sha256,
                    "rgb_unsigned_header_hex": unsigned_header.hex(),
                    "rgb_width": header.width,
                    "rgb_height": header.height,
                    "rgb_stride": header.stride,
                    "rgb_payload_bytes": len(payload),
                    "depth_payload_path": f"depth/{depth_name}",
                    "depth_payload_sha256": _sha256_bytes(depth_wire.payload),
                    "depth_record_sha256": depth_wire.record_sha256,
                    "depth_source_stamp_ns": depth_wire.source_stamp_ns,
                    "depth_width": depth_wire.width,
                    "depth_height": depth_wire.height,
                    "depth_stride": depth_wire.stride_bytes,
                    "depth_encoding": depth_wire.encoding,
                    "depth_metadata": depth_wire.metadata,
                    "camera_info_stamp_ns": header.camera_info_stamp_ns,
                    "navsat_stamp_ns": header.navsat_stamp_ns,
                    "odometry_stamp_ns": header.odometry_stamp_ns,
                    "latitude_deg": header.latitude_deg,
                    "longitude_deg": header.longitude_deg,
                    "altitude_m": header.altitude_m,
                    "position_east_m": header.position_east_m,
                    "position_north_m": header.position_north_m,
                    "position_up_m": header.position_up_m,
                    "orientation_xyzw": [
                        header.orientation_x,
                        header.orientation_y,
                        header.orientation_z,
                        header.orientation_w,
                    ],
                    "velocity_forward_left_up_mps": [
                        header.velocity_forward_mps,
                        header.velocity_left_mps,
                        header.velocity_up_mps,
                    ],
                    "attitude_covariance_enu_rad2": list(header.attitude_covariance_enu_rad2),
                    "flags": header.flags,
                    "pixel_format": header.pixel_format,
                    "annotation_topics_joined": False,
                }
            )
        if stream.read(1):
            raise ValueError("sensor stream contains trailing bytes after the requested frames")

    for path, original in zip(input_paths, input_files, strict=True):
        if _file_binding(path)["sha256"] != original["sha256"]:
            raise ValueError("source evidence changed during offline extraction")
    if _verify_annotation_binding(annotation_binding_path, annotation_config_path) != binding:
        raise ValueError("annotation preparation assets changed during extraction")

    manifest = {
        "schema": "multidetect.gazebo-annotation-sensor-staging",
        "schema_version": 1,
        "contract": "multidetect.gazebo-annotation-sensor-staging-v1",
        "source": {
            "stream_path": stream_path.as_posix(),
            "stream_sha256": _sha256(stream_path),
            "depth_contract_path": depth_contract_path.as_posix(),
            "depth_contract_sha256": _sha256(depth_contract_path),
            "captured_at_clock": "gazebo_sensor",
            "simulation_time_only": True,
        },
        "annotation_binding": binding,
        "annotation_config": annotation_config,
        "camera_evidence": camera_evidence,
        "depth_contract": depth_contract,
        "frames": frames,
        "runtime_boundary": {
            "policy_input_contract": "rgb_depth_camera_info_pose_telemetry_only",
            "runtime_consumes_annotation_topics": False,
            "truth_injected_into_policy": False,
            "offline_staging_only": True,
        },
        "training_ready": False,
        "capture_authenticated": False,
        "production_mainline_asset_binding": "not_claimed",
        "training_blocker": (
            "exact annotation RGB hash and Gazebo sensor timestamp joins are still required; "
            "authenticated capture receipt, runtime asset attestation and independent review "
            "are missing; no host clock or production asset binding is claimed"
        ),
    }
    manifest_path = output_root / "staging-manifest.json"
    _write_json_exclusive(manifest_path, manifest)
    receipt = {
        "schema": "multidetect.gazebo-annotation-sensor-staging-receipt",
        "schema_version": 1,
        "contract": "multidetect.gazebo-annotation-sensor-staging-receipt-v1",
        "stream_sha256": _sha256(stream_path),
        "stream_path": stream_path.as_posix(),
        "frame_count": frame_count,
        "staging_manifest": manifest_path.as_posix(),
        "staging_manifest_sha256": _sha256(manifest_path),
        "annotation_binding_sha256": str(binding["binding_sha256"]),
        "annotation_config_sha256": str(annotation_config["sha256"]),
        "camera_evidence_sha256": camera_evidence["evidence_file"]["sha256"],
        "camera_calibration_sha256": camera_evidence["expected_calibration_file"]["sha256"],
        "annotation_resource_assets_sha256": binding["resource_root"]["assets_sha256"],
        "captured_at_clock": "gazebo_sensor",
        "annotation_topics_joined": False,
        "runtime_truth_free": True,
        "training_ready": False,
        "capture_authenticated": False,
        "production_mainline_asset_binding": "not_claimed",
        "external_gazebo_runtime": "supplied_sensor_stream_and_log_not_capture_attested",
        "hardware": "not_claimed",
        "hil": "not_claimed",
        "real": "not_claimed",
        "outdoor_gnss": "not_claimed",
        "final_release_allowed": False,
    }
    _write_json_exclusive(output_root / "staging-receipt.json", receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", type=Path, default=DEFAULT_STREAM)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--depth-contract", type=Path, default=DEFAULT_DEPTH_CONTRACT)
    parser.add_argument("--annotation-binding", type=Path, default=DEFAULT_BINDING)
    parser.add_argument("--annotation-config", type=Path, default=DEFAULT_ANNOTATION_CONFIG)
    parser.add_argument("--camera-evidence", type=Path, default=DEFAULT_CAMERA_EVIDENCE)
    parser.add_argument("--camera-calibration", type=Path, default=DEFAULT_CAMERA_CALIBRATION)
    parser.add_argument("--frames", type=int, default=20)
    args = parser.parse_args()
    receipt = materialize(
        stream_path=args.stream,
        output_root=args.output_root,
        depth_contract_path=args.depth_contract,
        annotation_binding_path=args.annotation_binding,
        annotation_config_path=args.annotation_config,
        camera_evidence_path=args.camera_evidence,
        camera_calibration_path=args.camera_calibration,
        frame_count=args.frames,
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
