from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import pytest

from multidetect.autonomy.serialization import canonical_sha256
from multidetect.backends import RawSensorReplayRecording
from multidetect.backends.raw_replay import ReplayFrameSource
from multidetect.clock import ManualClock
from scripts.build_gazebo_raw_recording_manifest import build_recording


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sealed(path: Path, document: dict[str, object]) -> str:
    digest = canonical_sha256(document)
    path.write_text(
        json.dumps({**document, "manifest_sha256": digest}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return digest


def _capture_manifests(root: Path) -> tuple[Path, Path]:
    rgb_root = root / "rgb-source"
    depth_root = root / "depth-source"
    rgb_root.mkdir()
    depth_root.mkdir()
    rgb_frames: list[dict[str, object]] = []
    depth_frames: list[dict[str, object]] = []
    # Match the real MDGZSIM3 producer: frame sequences are zero based.
    for sequence in (0, 1):
        rgb_payload = bytes((sequence, 2, 3, 4, 5, 6))
        depth_payload = struct.pack("<2f", 1.0 + sequence, 1.0 + sequence)
        rgb_name = f"frame-{sequence:06d}.bgr24"
        depth_name = f"depth-{sequence:06d}.float32_m"
        (rgb_root / rgb_name).write_bytes(rgb_payload)
        (depth_root / depth_name).write_bytes(depth_payload)
        captured_at_s = float(sequence)
        record_hash = _sha(f"rgb-wire-{sequence}".encode())
        telemetry = {
            "latitude_deg": 47.397 + sequence * 0.000001,
            "longitude_deg": 8.546 + sequence * 0.000001,
            "gps_absolute_altitude_m": 500.0 + sequence,
            "altitude_agl_m": 30.0,
            "altitude_reference": "terrain_dem",
            "local_north_m": float(sequence),
            "local_east_m": float(sequence) + 0.5,
            "local_down_m": -30.0,
            "roll_deg": 1.0,
            "pitch_deg": -2.0,
            "heading_deg": 90.0,
            "attitude_roll_sigma_deg": 0.1,
            "attitude_pitch_sigma_deg": 0.1,
            "attitude_heading_sigma_deg": 0.2,
            "attitude_uncertainty_source": "gazebo_covariance",
            "velocity_north_mps": 2.0,
            "velocity_east_mps": 1.0,
            "gps_fix_type": 3,
            "satellites_visible": 12,
            "gps_horizontal_accuracy_m": 1.5,
            "gps_vertical_accuracy_m": 2.5,
            "gps_observed_at_s": captured_at_s,
            "attitude_observed_at_s": captured_at_s,
            "velocity_observed_at_s": captured_at_s,
            "altitude_observed_at_s": captured_at_s,
        }
        provenance = {
            "frame_id": f"gazebo-frame-{sequence:06d}",
            "sequence": sequence,
            "captured_at_s": captured_at_s,
            "width": 2,
            "height": 1,
            "record_sha256": record_hash,
            "frame_telemetry": telemetry,
        }
        rgb_frames.append(
            {
                "frame_id": provenance["frame_id"],
                "sequence": sequence,
                "file": rgb_name,
                "bytes": len(rgb_payload),
                "sha256": _sha(rgb_payload),
                "wire_record_sha256": record_hash,
                "provenance": provenance,
            }
        )
        depth_frames.append(
            {
                "frame_id": provenance["frame_id"],
                "sequence": sequence,
                "file": depth_name,
                "bytes": len(depth_payload),
                "sha256": _sha(depth_payload),
                "wire_record_sha256": _sha(f"depth-wire-{sequence}".encode()),
                "rgb_record_sha256": record_hash,
                "encoding": "FLOAT32_M",
                "width": 2,
                "height": 1,
            }
        )
    scenario_hash = "a" * 64
    rgb_document: dict[str, object] = {
        "schema": "multidetect.gazebo-bgr24-frame-set",
        "schema_version": 2,
        "generated_at_utc": "2026-10-06T00:00:00Z",
        "scenario_id": "urban-inspection-multirotor-001",
        "scenario_manifest_hash": scenario_hash,
        "wire_version": 3,
        "pixel_format": "BGR24",
        "source_attribution": {
            "pixels": "actual_gazebo_camera_sensor_output",
            "wire": "MDGZSIM3",
            "truth_loaded_by_runtime": False,
        },
        "claims": {"actual_gazebo_pixels_persisted": True},
        "mainline_asset_binding": {"world_sdf_sha256": "b" * 64},
        "frames": rgb_frames,
    }
    rgb_hash = _sealed(rgb_root / "gazebo-bgr24-frame-set.manifest.json", rgb_document)
    depth_document: dict[str, object] = {
        "schema": "multidetect.gazebo-depth-companion-set",
        "schema_version": 2,
        "wire": "MDGZDEP1",
        "source_attribution": {
            "depth": "actual_gazebo_depth_sensor_output",
            "truth_loaded_by_runtime": False,
        },
        "rgb_frame_set_manifest": {"manifest_sha256": rgb_hash},
        "frames": depth_frames,
    }
    _sealed(depth_root / "gazebo-depth-companion-set.manifest.json", depth_document)
    return (
        rgb_root / "gazebo-bgr24-frame-set.manifest.json",
        depth_root / "gazebo-depth-companion-set.manifest.json",
    )


def test_modern_gazebo_capture_binds_exact_bgr24_and_depth(tmp_path: Path) -> None:
    rgb, depth = _capture_manifests(tmp_path)
    result = build_recording(
        rgb,
        depth,
        tmp_path / "recording",
        recording_id="gazebo-modern-fixture",
        airframe_profile_hash="c" * 64,
        scenario_seed=7,
    )

    assert result["training_ready"] is False
    assert result["truth_free"] is True
    manifest_path = Path(result["recording_manifest"])
    recording = RawSensorReplayRecording.from_manifest(manifest_path)
    assert len(recording.frames) == 2
    assert recording.frames[0].image.encoding == "png"
    assert len(recording.telemetry_samples) == 2
    assert Path(result["binding"]).is_file()
    source = ReplayFrameSource(recording, ManualClock(initial_monotonic_s=0.0))
    source.open()
    frame = source.read()
    assert tuple(frame.image_bgr.shape) == (1, 2, 3)
    assert bytes(frame.image_bgr.reshape(-1)) == bytes((0, 2, 3, 4, 5, 6))


def test_modern_gazebo_capture_rejects_runtime_truth(tmp_path: Path) -> None:
    rgb, depth = _capture_manifests(tmp_path)
    document = json.loads(rgb.read_text(encoding="utf-8"))
    document["source_attribution"]["truth_loaded_by_runtime"] = True
    document.pop("manifest_sha256")
    _sealed(rgb, document)

    with pytest.raises(ValueError, match="truth-free"):
        build_recording(
            rgb,
            depth,
            tmp_path / "recording",
            recording_id="rejected",
            airframe_profile_hash="c" * 64,
            scenario_seed=7,
        )


def test_real_legacy_asset_provenance_is_replayable_but_explicitly_not_current_mainline(
    tmp_path: Path,
) -> None:
    rgb, depth = _capture_manifests(tmp_path)
    document = json.loads(rgb.read_text(encoding="utf-8"))
    document.pop("mainline_asset_binding")
    document.pop("manifest_sha256", None)
    document["repository_model_assets"] = {"assets_sha256": "d" * 64}
    document["terrain_dem"] = {"file_sha256": "e" * 64}
    _sealed(rgb, document)
    depth = depth.resolve()
    depth_document = json.loads(depth.read_text(encoding="utf-8"))
    depth_document["schema_version"] = 1
    depth_document["rgb_frame_set_manifest"]["manifest_sha256"] = json.loads(
        rgb.read_text(encoding="utf-8")
    )["manifest_sha256"]
    depth_document.pop("manifest_sha256", None)
    _sealed(depth, depth_document)

    result = build_recording(
        rgb,
        depth,
        tmp_path / "recording",
        recording_id="legacy-real-gazebo-capture",
        airframe_profile_hash="c" * 64,
        scenario_seed=7,
    )
    binding = json.loads(Path(result["binding"]).read_text(encoding="utf-8"))
    assert binding["source_asset_binding"] == {
        "mode": "legacy_repository_assets_and_terrain",
        "repository_model_assets_sha256": "d" * 64,
        "terrain_dem_sha256": "e" * 64,
        "training_eligibility": "strict_current_mainline_binding_required",
    }
