"""Bind a modern Gazebo RGB/depth capture to the raw replay contract.

This converter is intentionally a capture-binding tool, not a label generator.
It consumes the hash-sealed ``MDGZSIM3`` RGB frame set produced by the external
Gazebo sensor path, preserves the exact BGR24 bytes as source evidence, and
creates a lossless PNG replay payload whose decoded pixels are identical. The
optional depth companion is copied and recorded in a sidecar binding so the
rendered-data and trajectory builders can use the same capture without loading
simulator truth.

The output remains training-ineligible until the existing independent review
and training gates pass.  No detector result, target label, or expected answer
is written here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from multidetect.autonomy.serialization import canonical_sha256  # noqa: E402
from multidetect.backends import RawSensorReplayRecording  # noqa: E402

RGB_SCHEMA = "multidetect.gazebo-bgr24-frame-set"
DEPTH_SCHEMA = "multidetect.gazebo-depth-companion-set"
RGB_WIRE = "MDGZSIM3"
DEPTH_WIRE = "MDGZDEP1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _finite(value: object, name: str, *, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be a finite number")
    return parsed


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _load_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"manifest must be a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"manifest is not valid UTF-8 JSON: {path}") from exc
    return _mapping(value, f"manifest {path}")


def _verify_manifest_hash(document: Mapping[str, Any], path: Path) -> str:
    claimed = _hash(document.get("manifest_sha256"), f"{path}.manifest_sha256")
    without_hash = dict(document)
    without_hash.pop("manifest_sha256", None)
    if canonical_sha256(without_hash) != claimed:
        raise ValueError(f"manifest canonical SHA-256 mismatch: {path}")
    return claimed


def _contained_source(root: Path, name: object, label: str) -> Path:
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise ValueError(f"{label} must be a basename")
    raw_source = root / name
    if raw_source.is_symlink() or not raw_source.is_file():
        raise ValueError(f"{label} must be a regular file: {raw_source}")
    source = raw_source.resolve(strict=True)
    try:
        source.relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise ValueError(f"{label} escapes its manifest directory") from exc
    return source


def _copy_exact(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
    expected_bytes: int,
) -> None:
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"refusing to overwrite recording asset: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    observed_bytes = destination.stat().st_size
    observed_sha256 = _sha256(destination)
    if observed_bytes != expected_bytes or observed_sha256 != expected_sha256:
        raise ValueError(f"copied asset failed integrity verification: {destination}")


def _encode_bgr24_png(
    source: Path,
    destination: Path,
    *,
    width: int,
    height: int,
    expected_sha256: str,
) -> tuple[int, str]:
    """Encode the raw BGR24 payload losslessly for the existing replay contract."""

    try:
        import cv2
        import numpy as np
    except ImportError as exc:  # pragma: no cover - optional vision dependency
        raise RuntimeError("Gazebo raw recording conversion requires OpenCV and NumPy") from exc
    payload = source.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValueError(f"source RGB payload changed before PNG conversion: {source}")
    expected_bytes = width * height * 3
    if len(payload) != expected_bytes:
        raise ValueError(f"source RGB BGR24 geometry does not match manifest: {source}")
    image = np.frombuffer(payload, dtype=np.uint8).reshape((height, width, 3))
    encoded_ok, encoded = cv2.imencode(".png", image)
    if not encoded_ok:
        raise RuntimeError(f"OpenCV failed to losslessly encode BGR24 frame: {source}")
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"refusing to overwrite recording asset: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(encoded.tobytes())
    return destination.stat().st_size, _sha256(destination)


def _frame_telemetry(record: Mapping[str, Any], *, captured_at_s: float) -> dict[str, Any]:
    provenance = _mapping(record.get("provenance"), "RGB frame provenance")
    source = _mapping(provenance.get("frame_telemetry"), "RGB frame telemetry")
    latitude = _finite(source.get("latitude_deg"), "latitude_deg")
    longitude = _finite(source.get("longitude_deg"), "longitude_deg")
    gps_altitude = _finite(source.get("gps_absolute_altitude_m"), "gps_absolute_altitude_m")
    velocity_north = _finite(source.get("velocity_north_mps"), "velocity_north_mps")
    velocity_east = _finite(source.get("velocity_east_mps"), "velocity_east_mps")
    assert latitude is not None
    assert longitude is not None
    assert gps_altitude is not None
    assert velocity_north is not None
    assert velocity_east is not None
    ground_speed = math.hypot(velocity_north, velocity_east)
    # These six fields are deliberately unknown: they are policy/vehicle state,
    # not measurements in the Gazebo camera frame.  Keeping them null prevents
    # a capture converter from inventing mission truth for training.
    return {
        "altitude_agl_m": source.get("altitude_agl_m"),
        "roll_deg": _finite(source.get("roll_deg"), "roll_deg"),
        "pitch_deg": _finite(source.get("pitch_deg"), "pitch_deg"),
        "ground_speed_mps": ground_speed,
        "in_allowed_zone": None,
        "geofence_healthy": None,
        "position_healthy": None,
        "link_healthy": None,
        "flight_mode_allows_deploy": None,
        "release_zone_clear": None,
        "person_detector_healthy": None,
        "latitude_deg": latitude,
        "longitude_deg": longitude,
        "heading_deg": _finite(source.get("heading_deg"), "heading_deg"),
        "battery_remaining_pct": None,
        "satellites_visible": _integer(source.get("satellites_visible"), "satellites_visible"),
        "gps_fix_type": _integer(source.get("gps_fix_type"), "gps_fix_type"),
        "gps_horizontal_accuracy_m": _finite(
            source.get("gps_horizontal_accuracy_m"), "gps_horizontal_accuracy_m"
        ),
        "gps_vertical_accuracy_m": _finite(
            source.get("gps_vertical_accuracy_m"), "gps_vertical_accuracy_m"
        ),
        "gps_observed_at_s": _finite(source.get("gps_observed_at_s"), "gps_observed_at_s"),
        "armed": None,
        "flight_mode": None,
        "mission_sequence": None,
        "attitude_observed_at_s": _finite(
            source.get("attitude_observed_at_s"), "attitude_observed_at_s"
        ),
        "attitude_roll_sigma_deg": _finite(
            source.get("attitude_roll_sigma_deg"), "attitude_roll_sigma_deg"
        ),
        "attitude_pitch_sigma_deg": _finite(
            source.get("attitude_pitch_sigma_deg"), "attitude_pitch_sigma_deg"
        ),
        "attitude_heading_sigma_deg": _finite(
            source.get("attitude_heading_sigma_deg"), "attitude_heading_sigma_deg"
        ),
        "attitude_uncertainty_source": source.get("attitude_uncertainty_source"),
        "position_observed_at_s": _finite(
            source.get("gps_observed_at_s"), "position_observed_at_s"
        ),
        "velocity_north_mps": velocity_north,
        "velocity_east_mps": velocity_east,
        "velocity_observed_at_s": _finite(
            source.get("velocity_observed_at_s"), "velocity_observed_at_s"
        ),
        "altitude_observed_at_s": source.get("altitude_observed_at_s"),
        "altitude_reference": source.get("altitude_reference"),
        "local_north_m": _finite(source.get("local_north_m"), "local_north_m"),
        "local_east_m": _finite(source.get("local_east_m"), "local_east_m"),
        "local_down_m": _finite(source.get("local_down_m"), "local_down_m"),
        "local_position_observed_at_s": _finite(
            source.get("gps_observed_at_s"), "local_position_observed_at_s"
        ),
        "gps_absolute_altitude_m": gps_altitude,
    }


def _verify_rgb_document(
    document: dict[str, Any], path: Path
) -> tuple[str, str, list[dict[str, Any]]]:
    _verify_manifest_hash(document, path)
    if document.get("schema") != RGB_SCHEMA or document.get("schema_version") != 2:
        raise ValueError("RGB manifest must be the modern MDGZSIM3 frame-set schema")
    if document.get("wire_version") != 3 or document.get("pixel_format") != "BGR24":
        raise ValueError("RGB manifest must bind MDGZSIM3 BGR24 payloads")
    source = _mapping(document.get("source_attribution"), "RGB source_attribution")
    if (
        source.get("pixels") != "actual_gazebo_camera_sensor_output"
        or source.get("wire") != RGB_WIRE
        or source.get("truth_loaded_by_runtime") is not False
    ):
        raise ValueError("RGB manifest is not truth-free actual Gazebo sensor output")
    claims = _mapping(document.get("claims"), "RGB claims")
    if claims.get("actual_gazebo_pixels_persisted") is not True:
        raise ValueError("RGB manifest does not claim persisted actual Gazebo pixels")
    scenario_id = document.get("scenario_id")
    scenario_hash = _hash(document.get("scenario_manifest_hash"), "scenario_manifest_hash")
    if not isinstance(scenario_id, str) or not scenario_id:
        raise ValueError("RGB scenario_id is required")
    bindings = document.get("mainline_asset_binding")
    if isinstance(bindings, dict):
        for name, value in bindings.items():
            _hash(value, f"mainline_asset_binding.{name}")
    else:
        # Older real MDGZSIM3 captures sealed the model and terrain bundle in
        # their source sections before the explicit mainline binding field was
        # introduced.  Accept that provenance for raw-recording/replay use,
        # but keep the distinction explicit; the strict training gate still
        # requires a current approved mainline binding later in the pipeline.
        model_assets = _mapping(
            document.get("repository_model_assets"), "repository_model_assets"
        )
        terrain = _mapping(document.get("terrain_dem"), "terrain_dem")
        _hash(model_assets.get("assets_sha256"), "repository_model_assets.assets_sha256")
        _hash(terrain.get("file_sha256"), "terrain_dem.file_sha256")
    frames = document.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ValueError("RGB manifest frames must be a non-empty array")
    normalized: list[dict[str, Any]] = []
    seen_sequences: set[int] = set()
    previous_time = -1.0
    for index, raw in enumerate(frames):
        record = _mapping(raw, f"RGB frame {index}")
        # Gazebo's MDGZSIM3 producer uses a zero-based sequence.  Do not
        # normalize it here: the sequence is part of the RGB/depth pair
        # identity and must remain exactly what the wire capture recorded.
        sequence = _integer(record.get("sequence"), f"RGB frame {index}.sequence", minimum=0)
        if sequence in seen_sequences:
            raise ValueError("RGB frame sequences must be unique")
        seen_sequences.add(sequence)
        frame_id = record.get("frame_id")
        if not isinstance(frame_id, str) or not frame_id:
            raise ValueError("RGB frame_id must be non-empty")
        provenance = _mapping(record.get("provenance"), f"RGB frame {index}.provenance")
        captured_at_s = _finite(provenance.get("captured_at_s"), "captured_at_s")
        assert captured_at_s is not None
        if captured_at_s <= previous_time:
            raise ValueError("RGB capture timestamps must be strictly increasing")
        previous_time = captured_at_s
        file_name = record.get("file")
        source_file = _contained_source(path.parent, file_name, f"RGB frame {index}.file")
        expected_bytes = _integer(record.get("bytes"), f"RGB frame {index}.bytes", minimum=1)
        expected_sha256 = _hash(record.get("sha256"), f"RGB frame {index}.sha256")
        if source_file.stat().st_size != expected_bytes or _sha256(source_file) != expected_sha256:
            raise ValueError(f"RGB frame payload failed manifest verification: {source_file}")
        width = _integer(provenance.get("width"), "RGB frame width", minimum=1)
        height = _integer(provenance.get("height"), "RGB frame height", minimum=1)
        if expected_bytes != width * height * 3:
            raise ValueError("RGB BGR24 payload size does not match frame geometry")
        if record.get("wire_record_sha256") != provenance.get("record_sha256"):
            raise ValueError("RGB frame wire record hash is not bound to provenance")
        normalized.append(
            {
                "record": record,
                "source_file": source_file,
                "frame_id": frame_id,
                "sequence": sequence,
                "captured_at_s": captured_at_s,
                "bytes": expected_bytes,
                "sha256": expected_sha256,
                "width": width,
                "height": height,
                "scenario_id": scenario_id,
                "scenario_manifest_hash": scenario_hash,
                "telemetry": _frame_telemetry(record, captured_at_s=captured_at_s),
            }
        )
    return scenario_id, scenario_hash, normalized


def _verify_depth_document(
    document: dict[str, Any],
    path: Path,
    *,
    rgb_manifest_hash: str,
    rgb_frames: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    _verify_manifest_hash(document, path)
    if document.get("schema") != DEPTH_SCHEMA or document.get("schema_version") not in {1, 2}:
        raise ValueError("depth manifest must be the supported MDGZDEP1 companion schema")
    if document.get("wire") != DEPTH_WIRE:
        raise ValueError("depth manifest must bind MDGZDEP1 payloads")
    source = _mapping(document.get("source_attribution"), "depth source_attribution")
    if source.get("depth") != "actual_gazebo_depth_sensor_output" or source.get(
        "truth_loaded_by_runtime"
    ) is not False:
        raise ValueError("depth manifest is not truth-free actual Gazebo sensor output")
    rgb_binding = _mapping(document.get("rgb_frame_set_manifest"), "depth RGB binding")
    if rgb_binding.get("manifest_sha256") != rgb_manifest_hash:
        raise ValueError("depth manifest is bound to a different RGB frame set")
    frames = document.get("frames")
    if not isinstance(frames, list) or len(frames) != len(rgb_frames):
        raise ValueError("RGB/depth frame counts do not match")
    result: list[dict[str, Any]] = []
    for index, (raw, rgb) in enumerate(zip(frames, rgb_frames, strict=True)):
        record = _mapping(raw, f"depth frame {index}")
        if (
            record.get("frame_id") != rgb["frame_id"]
            or record.get("sequence") != rgb["sequence"]
            or record.get("rgb_record_sha256") != rgb["record"].get("wire_record_sha256")
        ):
            raise ValueError(f"RGB/depth pair binding mismatch at frame {index}")
        source_file = _contained_source(
            path.parent, record.get("file"), f"depth frame {index}.file"
        )
        expected_bytes = _integer(record.get("bytes"), f"depth frame {index}.bytes", minimum=1)
        expected_sha256 = _hash(record.get("sha256"), f"depth frame {index}.sha256")
        if (
            record.get("encoding") != "FLOAT32_M"
            or record.get("width") != rgb["width"]
            or record.get("height") != rgb["height"]
            or expected_bytes != rgb["width"] * rgb["height"] * 4
        ):
            raise ValueError("depth FLOAT32 payload encoding/geometry does not match RGB")
        if source_file.stat().st_size != expected_bytes or _sha256(source_file) != expected_sha256:
            raise ValueError(f"depth payload failed manifest verification: {source_file}")
        result.append(
            {
                "frame_id": rgb["frame_id"],
                "sequence": rgb["sequence"],
                "source_file": source_file,
                "bytes": expected_bytes,
                "sha256": expected_sha256,
                "encoding": record.get("encoding"),
                "wire_record_sha256": record.get("wire_record_sha256"),
                "rgb_record_sha256": record.get("rgb_record_sha256"),
            }
        )
    return result


def _parse_started_at(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("started_at_utc must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("started_at_utc must include a UTC offset")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_recording(
    rgb_manifest: Path,
    depth_manifest: Path,
    output_directory: Path,
    *,
    recording_id: str,
    airframe_profile_hash: str,
    scenario_seed: int,
    started_at_utc: str | None = None,
) -> dict[str, Any]:
    if output_directory.is_symlink():
        raise ValueError(f"output directory must not be a symlink: {output_directory}")
    if output_directory.exists() and any(output_directory.iterdir()):
        raise ValueError(f"output directory must be empty or absent: {output_directory}")
    output_directory.mkdir(parents=True, exist_ok=True)
    rgb_path = rgb_manifest.resolve(strict=True)
    depth_path = depth_manifest.resolve(strict=True)
    rgb_document = _load_json(rgb_path)
    scenario_id, scenario_hash, rgb_frames = _verify_rgb_document(rgb_document, rgb_path)
    depth_document = _load_json(depth_path)
    depth_frames = _verify_depth_document(
        depth_document,
        depth_path,
        rgb_manifest_hash=_hash(rgb_document.get("manifest_sha256"), "RGB manifest hash"),
        rgb_frames=rgb_frames,
    )
    _hash(airframe_profile_hash, "airframe_profile_hash")
    if isinstance(scenario_seed, bool) or not isinstance(scenario_seed, int) or scenario_seed < 0:
        raise ValueError("scenario_seed must be a non-negative integer")
    if not isinstance(recording_id, str) or not recording_id.strip():
        raise ValueError("recording_id must be non-empty")
    started = _parse_started_at(started_at_utc) if started_at_utc else _parse_started_at(
        str(rgb_document.get("generated_at_utc"))
    )

    events: list[dict[str, Any]] = []
    binding_frames: list[dict[str, Any]] = []
    for index, (rgb, depth) in enumerate(zip(rgb_frames, depth_frames, strict=True), start=1):
        source_rgb_destination = output_directory / "source-rgb" / f"frame-{index:06d}.bgr24"
        _copy_exact(
            rgb["source_file"],
            source_rgb_destination,
            expected_sha256=rgb["sha256"],
            expected_bytes=rgb["bytes"],
        )
        rgb_destination = output_directory / "rgb" / f"frame-{index:06d}.png"
        replay_bytes, replay_sha256 = _encode_bgr24_png(
            source_rgb_destination,
            rgb_destination,
            width=rgb["width"],
            height=rgb["height"],
            expected_sha256=rgb["sha256"],
        )
        depth_destination = output_directory / "depth" / depth["source_file"].name
        _copy_exact(
            depth["source_file"],
            depth_destination,
            expected_sha256=depth["sha256"],
            expected_bytes=depth["bytes"],
        )
        captured_at_s = rgb["captured_at_s"]
        events.append(
            {
                "schema_version": 1,
                "kind": "telemetry",
                "observed_at_s": captured_at_s,
                "telemetry": rgb["telemetry"],
            }
        )
        events.append(
            {
                "schema_version": 1,
                "kind": "frame",
                "frame_id": rgb["frame_id"],
                "captured_at_s": captured_at_s,
                "image": {
                    "path": rgb_destination.relative_to(output_directory).as_posix(),
                    "sha256": replay_sha256,
                    "bytes": replay_bytes,
                    "width": rgb["width"],
                    "height": rgb["height"],
                    "encoding": "png",
                },
            }
        )
        binding_frames.append(
            {
                "frame_id": rgb["frame_id"],
                "sequence": rgb["sequence"],
                "captured_at_s": captured_at_s,
                "rgb_source_file": rgb["source_file"].name,
                "rgb_source_sha256": rgb["sha256"],
                "rgb_replay_file": rgb_destination.relative_to(output_directory).as_posix(),
                "rgb_replay_sha256": replay_sha256,
                "rgb_replay_bytes": replay_bytes,
                "rgb_wire_record_sha256": rgb["record"].get("wire_record_sha256"),
                "depth_source_file": depth["source_file"].name,
                "depth_source_sha256": depth["sha256"],
                "depth_wire_record_sha256": depth["wire_record_sha256"],
                "rgb_depth_pair_sha256": canonical_sha256(
                    {
                        "frame_id": rgb["frame_id"],
                        "sequence": rgb["sequence"],
                        "rgb_record_sha256": rgb["record"].get("wire_record_sha256"),
                        "depth_record_sha256": depth["wire_record_sha256"],
                    }
                ),
            }
        )

    events_path = output_directory / "events.jsonl"
    event_bytes = b"".join(
        (
            json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        for event in events
    )
    events_path.write_bytes(event_bytes)
    event_sha256 = hashlib.sha256(event_bytes).hexdigest()
    recording = RawSensorReplayRecording.from_jsonl(
        events_path,
        recording_id=recording_id,
        airframe_profile_hash=airframe_profile_hash,
        started_at_utc=started,
        expected_sha256=event_sha256,
        expected_byte_count=len(event_bytes),
        scenario_id=scenario_id,
        scenario_manifest_hash=scenario_hash,
        scenario_seed=scenario_seed,
    )
    manifest = {
        "schema_version": 1,
        "recording_id": recording_id,
        "airframe_profile_hash": airframe_profile_hash,
        "started_at_utc": started,
        "recording_hash": recording.recording_hash,
        "events": {
            "path": "events.jsonl",
            "sha256": event_sha256,
            "bytes": len(event_bytes),
        },
        "scenario": {
            "scenario_id": scenario_id,
            "scenario_manifest_hash": scenario_hash,
            "scenario_seed": scenario_seed,
        },
    }
    manifest_path = output_directory / "recording.manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    reloaded = RawSensorReplayRecording.from_manifest(manifest_path)
    if reloaded.recording_hash != recording.recording_hash:
        raise ValueError("recording manifest did not round-trip its recording hash")
    binding = {
        "schema": "multidetect.gazebo-modern-raw-recording-binding",
        "schema_version": 1,
        "recording_manifest_sha256": _sha256(manifest_path),
        "recording_hash": recording.recording_hash,
        "rgb_manifest": {
            "path": str(rgb_path),
            "file_sha256": _sha256(rgb_path),
            "manifest_sha256": rgb_document["manifest_sha256"],
        },
        "depth_manifest": {
            "path": str(depth_path),
            "file_sha256": _sha256(depth_path),
            "manifest_sha256": depth_document["manifest_sha256"],
        },
        "scenario": {
            "scenario_id": scenario_id,
            "scenario_manifest_hash": scenario_hash,
            "scenario_seed": scenario_seed,
        },
        "source_attribution": {
            "rgb": "actual_gazebo_camera_sensor_output",
            "depth": "actual_gazebo_depth_sensor_output",
            "truth_loaded_by_runtime": False,
            "training_ready": False,
            "independent_review": "required",
        },
        "source_asset_binding": (
            {
                "mode": "explicit_mainline_asset_binding",
                "binding": rgb_document.get("mainline_asset_binding"),
            }
            if isinstance(rgb_document.get("mainline_asset_binding"), dict)
            else {
                "mode": "legacy_repository_assets_and_terrain",
                "repository_model_assets_sha256": rgb_document[
                    "repository_model_assets"
                ]["assets_sha256"],
                "terrain_dem_sha256": rgb_document["terrain_dem"]["file_sha256"],
                "training_eligibility": "strict_current_mainline_binding_required",
            }
        ),
        "frames": binding_frames,
    }
    binding["binding_sha256"] = canonical_sha256(binding)
    binding_path = output_directory / "gazebo_capture_binding.json"
    binding_path.write_text(
        json.dumps(binding, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    observed_binding = _load_json(binding_path)
    observed_binding_hash = observed_binding.pop("binding_sha256", None)
    expected_binding_hash = binding["binding_sha256"]
    if observed_binding_hash != expected_binding_hash or canonical_sha256(observed_binding) != (
        expected_binding_hash
    ):
        raise ValueError("Gazebo capture binding hash verification failed")
    return {
        "recording_manifest": str(manifest_path),
        "recording_manifest_sha256": _sha256(manifest_path),
        "recording_hash": recording.recording_hash,
        "binding": str(binding_path),
        "binding_sha256": binding["binding_sha256"],
        "frame_count": len(recording.frames),
        "telemetry_count": len(recording.telemetry_samples),
        "depth_count": len(depth_frames),
        "training_ready": False,
        "truth_free": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rgb-manifest", required=True, type=Path)
    parser.add_argument("--depth-manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--recording-id", required=True)
    parser.add_argument("--airframe-profile-hash", required=True)
    parser.add_argument("--scenario-seed", required=True, type=int)
    parser.add_argument("--started-at-utc")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = build_recording(
            args.rgb_manifest,
            args.depth_manifest,
            args.output,
            recording_id=args.recording_id,
            airframe_profile_hash=args.airframe_profile_hash,
            scenario_seed=args.scenario_seed,
            started_at_utc=args.started_at_utc,
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
