#!/usr/bin/env python3
"""Bind a persisted Gazebo camera capture to the raw-sensor replay contract.

The input is the output directory of ``gazebo_camera_capture.py`` run with
``--persist-frames``.  The converter does not read Gazebo world/model truth or
detector output.  It only re-encodes the received pixels and carries forward
the newest NavSat sample as read-only telemetry, which lets the existing
production perception/live core and QGC RTSP barrier consume the same frames.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2

from multidetect.backends import RawSensorReplayRecording
from multidetect.product_version import load_product_version
from multidetect.synthetic_model import create_synthetic_hil_model_bundle


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _require_int(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _require_real(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _navsat_from_frame(frame: dict[str, Any], capture: dict[str, Any]) -> dict[str, float] | None:
    value = frame.get("navsat")
    if not isinstance(value, dict):
        latest = capture.get("latest_telemetry")
        value = latest.get("navsat") if isinstance(latest, dict) else None
    if not isinstance(value, dict):
        return None
    try:
        latitude = _require_real(value.get("latitude_deg"), "navsat.latitude_deg")
        longitude = _require_real(value.get("longitude_deg"), "navsat.longitude_deg")
        altitude = _require_real(value.get("altitude_m"), "navsat.altitude_m")
        # Keep the raw Gazebo adapter on the same geodetic wire contract as
        # MAVLink/QGC: the exact poles have no stable local longitude axis,
        # and absurd altitudes should never become a healthy replay sample.
        if not -90.0 < latitude < 90.0:
            raise ValueError("navsat.latitude_deg is outside (-90, 90)")
        if not -180.0 <= longitude <= 180.0:
            raise ValueError("navsat.longitude_deg is outside [-180, 180]")
        if not -1_000.0 <= altitude <= 100_000.0:
            raise ValueError("navsat.altitude_m exceeds the wire safety bound")
        return {
            "latitude_deg": latitude,
            "longitude_deg": longitude,
            "altitude_m": altitude,
        }
    except ValueError:
        return None


def _telemetry(captured_at_s: float, navsat: dict[str, float] | None) -> dict[str, object]:
    # These are the fields required by VehicleTelemetry.  Flight-control
    # permissions remain false; the bridge is a read-only replay adapter.
    sample: dict[str, object] = {
        "altitude_agl_m": max(0.0, navsat["altitude_m"] if navsat else 0.0),
        "roll_deg": 0.0,
        "pitch_deg": 0.0,
        "ground_speed_mps": 0.0,
        "in_allowed_zone": True,
        "geofence_healthy": True,
        "position_healthy": navsat is not None,
        "link_healthy": True,
        "flight_mode_allows_deploy": False,
        "release_zone_clear": None,
        "gps_fix_type": 3 if navsat else None,
        "satellites_visible": 12 if navsat else None,
        "gps_horizontal_accuracy_m": 1.5 if navsat else None,
        "gps_vertical_accuracy_m": 2.5 if navsat else None,
        "gps_observed_at_s": captured_at_s,
        "attitude_observed_at_s": captured_at_s,
        "position_observed_at_s": captured_at_s,
        "flight_mode": "HOLD",
        "armed": False,
    }
    if navsat is not None:
        sample.update(
            {
                "latitude_deg": navsat["latitude_deg"],
                "longitude_deg": navsat["longitude_deg"],
            }
        )
    return sample


def create_fixture(
    capture_directory: Path,
    output: Path,
    *,
    recording_id: str = "gazebo-harmonic-camera-replay",
    frame_limit: int | None = None,
) -> dict[str, object]:
    capture_directory = capture_directory.resolve(strict=True)
    capture_path = capture_directory / "capture.json"
    capture = _load_json(capture_path)
    if capture.get("schema") != "multidetect.gazebo-camera-capture":
        raise ValueError("capture.json is not a Gazebo camera capture")
    if capture.get("gate_passed") is not True:
        raise ValueError("Gazebo camera capture gate did not pass")
    frames_raw = capture.get("frames")
    if not isinstance(frames_raw, list) or not frames_raw:
        raise ValueError("capture.json contains no image frames")
    frames = frames_raw[:frame_limit] if frame_limit is not None else frames_raw
    if len(frames) < 3:
        raise ValueError("at least three persisted Gazebo frames are required")
    if output.exists():
        raise FileExistsError(f"fixture output already exists: {output}")

    recording_directory = output / "recording"
    model_directory = output / "model"
    recording_directory.mkdir(parents=True)
    events: list[dict[str, object]] = []
    first_stamp_ns = _require_int(frames[0].get("stamp_ns"), "frames[0].stamp_ns")
    previous_captured_at_s = 0.0
    telemetry_fallback_count = 0
    invalid_navsat_count = 0
    width = _require_int(frames[0].get("width"), "frames[0].width", minimum=1)
    height = _require_int(frames[0].get("height"), "frames[0].height", minimum=1)

    for index, raw_frame in enumerate(frames):
        if not isinstance(raw_frame, dict):
            raise ValueError(f"frame {index} must be an object")
        persisted_path = raw_frame.get("persisted_path")
        if not isinstance(persisted_path, str) or not persisted_path:
            raise ValueError(
                "capture must be generated with --persist-frames; missing persisted_path"
            )
        source_path = (capture_directory / persisted_path).resolve(strict=True)
        try:
            source_path.relative_to(capture_directory)
        except ValueError as exc:
            raise ValueError("persisted Gazebo frame escapes capture directory") from exc
        image = cv2.imread(str(source_path), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError(f"OpenCV could not decode Gazebo frame: {source_path}")
        observed_width = int(image.shape[1])
        observed_height = int(image.shape[0])
        if observed_width != width or observed_height != height:
            raise ValueError("Gazebo frames do not share one stable geometry")
        captured_at_s = (
            1.0
            + (
                _require_int(raw_frame.get("stamp_ns"), f"frames[{index}].stamp_ns")
                - first_stamp_ns
            )
            / 1_000_000_000.0
        )
        if captured_at_s <= previous_captured_at_s:
            raise ValueError("Gazebo frame timestamps must be strictly increasing")
        previous_captured_at_s = captured_at_s

        encoded_ok, encoded = cv2.imencode(".png", image)
        if not encoded_ok:
            raise RuntimeError(f"OpenCV failed to encode Gazebo frame {index}")
        payload = encoded.tobytes()
        image_name = f"frame-{index:04d}.png"
        (recording_directory / image_name).write_bytes(payload)
        navsat = _navsat_from_frame(raw_frame, capture)
        if not isinstance(raw_frame.get("navsat"), dict):
            telemetry_fallback_count += 1
        elif navsat is None:
            invalid_navsat_count += 1
        telemetry_at_s = max(0.0, captured_at_s - 0.001)
        events.append(
            {
                "schema_version": 1,
                "kind": "telemetry",
                "observed_at_s": telemetry_at_s,
                "telemetry": _telemetry(telemetry_at_s, navsat),
            }
        )
        events.append(
            {
                "schema_version": 1,
                "kind": "frame",
                "frame_id": f"gazebo-frame-{index:04d}",
                "captured_at_s": captured_at_s,
                "image": {
                    "path": image_name,
                    "sha256": _sha256(payload),
                    "bytes": len(payload),
                    "width": width,
                    "height": height,
                    "encoding": "png",
                },
            }
        )

    event_payload = "".join(
        json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n" for item in events
    ).encode("utf-8")
    event_path = recording_directory / "events.jsonl"
    event_path.write_bytes(event_payload)
    event_sha256 = _sha256(event_payload)
    started_at = datetime(2026, 8, 25, tzinfo=timezone.utc)
    recording = RawSensorReplayRecording.from_jsonl(
        event_path,
        recording_id=recording_id,
        airframe_profile_hash="b" * 64,
        started_at_utc=started_at,
        expected_sha256=event_sha256,
        expected_byte_count=len(event_payload),
    )
    manifest = {
        "schema_version": 1,
        "recording_id": recording.recording_id,
        "airframe_profile_hash": recording.airframe_profile_hash,
        "started_at_utc": started_at.isoformat().replace("+00:00", "Z"),
        "recording_hash": recording.recording_hash,
        "events": {
            "path": event_path.name,
            "sha256": event_sha256,
            "bytes": len(event_payload),
        },
    }
    manifest_payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest_path = recording_directory / "recording.manifest.json"
    manifest_path.write_bytes(manifest_payload)
    model_path, model_manifest_path = create_synthetic_hil_model_bundle(
        model_directory,
        input_width=width,
        input_height=height,
    )
    product = load_product_version()
    capture_bytes = capture_path.read_bytes()
    summary: dict[str, object] = {
        "schema_version": 1,
        "fixture_type": "multidetect.gazebo-harmonic-raw-replay",
        "release_label": product.release_label,
        "python_version": product.python,
        "source_capture": str(capture_path),
        "source_capture_sha256": _sha256(capture_bytes),
        "recording_manifest": str(manifest_path.resolve()),
        "recording_manifest_sha256": _sha256(manifest_payload),
        "recording_hash": recording.recording_hash,
        "events_sha256": event_sha256,
        "model": str(model_path.resolve()),
        "model_sha256": _sha256(model_path.read_bytes()),
        "model_manifest": str(model_manifest_path.resolve()),
        "model_manifest_sha256": _sha256(model_manifest_path.read_bytes()),
        "frame_count": len(recording.frames),
        "width": width,
        "height": height,
        "camera_topic": capture.get("image_topic"),
        "navsat_topic": capture.get("navsat_topic"),
        "model_instance": capture.get("model_instance"),
        "telemetry_fallback_count": telemetry_fallback_count,
        "invalid_navsat_count": invalid_navsat_count,
        "ground_truth_present": False,
        "hardware_outputs_present": False,
    }
    summary_path = output / "fixture.json"
    summary["fixture_summary"] = str(summary_path.resolve())
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("capture_directory", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--recording-id", default="gazebo-harmonic-camera-replay")
    parser.add_argument("--frame-limit", type=int)
    args = parser.parse_args()
    if args.frame_limit is not None and args.frame_limit < 3:
        raise SystemExit("--frame-limit must be at least 3")
    result = create_fixture(
        args.capture_directory,
        args.output.resolve(),
        recording_id=args.recording_id,
        frame_limit=args.frame_limit,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
