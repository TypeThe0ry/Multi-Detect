#!/usr/bin/env python3
"""Create a deterministic raw-sensor + synthetic-model E4 acceptance fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from multidetect.backends import RawSensorReplayRecording
from multidetect.product_version import load_product_version
from multidetect.synthetic_model import create_synthetic_hil_model_bundle


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _telemetry(observed_at_s: float, index: int) -> dict[str, object]:
    return {
        "altitude_agl_m": 24.0 + math.sin(index / 9.0),
        "roll_deg": 1.5 * math.sin(index / 7.0),
        "pitch_deg": -2.0 + 0.5 * math.cos(index / 8.0),
        "ground_speed_mps": 16.0,
        "in_allowed_zone": True,
        "geofence_healthy": True,
        "position_healthy": True,
        "link_healthy": True,
        "flight_mode_allows_deploy": False,
        "release_zone_clear": None,
        "latitude_deg": 1.3521 + index * 0.000001,
        "longitude_deg": 103.8198 + index * 0.0000015,
        "gps_fix_type": 3,
        "satellites_visible": 16,
        "altitude_observed_at_s": observed_at_s,
        "attitude_observed_at_s": observed_at_s,
        "position_observed_at_s": observed_at_s,
    }


def _frame(width: int, height: int, index: int, frame_count: int) -> np.ndarray:
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, :, 0] = np.linspace(18, 75, width, dtype=np.uint8)
    image[:, :, 1] = np.linspace(55, 18, height, dtype=np.uint8)[:, None]
    image[:, :, 2] = 24
    span_x = max(1, width - 96)
    span_y = max(1, height - 120)
    x = 32 + round(span_x * index / max(1, frame_count - 1))
    y = 50 + round(span_y * (0.5 + 0.35 * math.sin(index / 4.0)))
    cv2.rectangle(image, (x - 28, y - 28), (x + 28, y + 28), (0, 110, 255), -1)
    cv2.rectangle(image, (x - 34, y - 34), (x + 34, y + 34), (255, 255, 255), 2)
    cv2.putText(
        image,
        f"REPLAY {index + 1:03d}/{frame_count:03d}",
        (12, 28),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.62,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    return image


def create_fixture(
    output: Path,
    *,
    frame_count: int,
    fps: float,
    width: int,
    height: int,
) -> dict[str, object]:
    if output.exists():
        raise FileExistsError(f"fixture output already exists: {output}")
    if frame_count <= 0:
        raise ValueError("frame_count must be positive")
    if not math.isfinite(fps) or fps <= 0.0:
        raise ValueError("fps must be finite and positive")
    if width <= 0 or height <= 0 or width % 2 or height % 2:
        raise ValueError("width and height must be positive even integers")

    recording_dir = output / "recording"
    model_dir = output / "model"
    recording_dir.mkdir(parents=True)
    events: list[dict[str, object]] = []
    first_capture_s = 1.0
    for index in range(frame_count):
        captured_at_s = first_capture_s + index / fps
        observed_at_s = max(0.0, captured_at_s - min(0.01, 0.25 / fps))
        events.append(
            {
                "schema_version": 1,
                "kind": "telemetry",
                "observed_at_s": observed_at_s,
                "telemetry": _telemetry(observed_at_s, index),
            }
        )
        image = _frame(width, height, index, frame_count)
        encoded_ok, encoded = cv2.imencode(".png", image)
        if not encoded_ok:
            raise RuntimeError(f"OpenCV failed to encode frame {index}")
        image_payload = encoded.tobytes()
        image_name = f"frame-{index:04d}.png"
        (recording_dir / image_name).write_bytes(image_payload)
        events.append(
            {
                "schema_version": 1,
                "kind": "frame",
                "frame_id": f"frame-{index:04d}",
                "captured_at_s": captured_at_s,
                "image": {
                    "path": image_name,
                    "sha256": _sha256(image_payload),
                    "bytes": len(image_payload),
                    "width": width,
                    "height": height,
                    "encoding": "png",
                },
            }
        )

    event_payload = "".join(
        json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n" for item in events
    ).encode("utf-8")
    event_path = recording_dir / "events.jsonl"
    event_path.write_bytes(event_payload)
    event_sha256 = _sha256(event_payload)
    started_at = datetime(2026, 8, 25, tzinfo=timezone.utc)
    recording = RawSensorReplayRecording.from_jsonl(
        event_path,
        recording_id="qgc-e4-synchronized-replay",
        airframe_profile_hash="a" * 64,
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
    manifest_path = recording_dir / "recording.manifest.json"
    manifest_path.write_bytes(manifest_payload)
    manifest_sha256 = _sha256(manifest_payload)
    model_path, model_manifest_path = create_synthetic_hil_model_bundle(
        model_dir,
        input_width=width,
        input_height=height,
    )
    product = load_product_version()
    result: dict[str, object] = {
        "schema_version": 1,
        "fixture_type": "multidetect.qgc-replay-acceptance",
        "release_label": product.release_label,
        "python_version": product.python,
        "recording_manifest": str(manifest_path.resolve()),
        "recording_manifest_sha256": manifest_sha256,
        "recording_hash": recording.recording_hash,
        "events_sha256": event_sha256,
        "model": str(model_path.resolve()),
        "model_sha256": _sha256(model_path.read_bytes()),
        "model_manifest": str(model_manifest_path.resolve()),
        "model_manifest_sha256": _sha256(model_manifest_path.read_bytes()),
        "frame_count": frame_count,
        "fps": fps,
        "width": width,
        "height": height,
        "synthetic_hil_only": True,
        "ground_truth_present": False,
        "hardware_outputs_present": False,
    }
    summary_path = output / "fixture.json"
    result["fixture_summary"] = str(summary_path.resolve())
    summary_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--frame-count", type=int, default=30)
    parser.add_argument("--fps", type=float, default=15.0)
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--height", type=int, default=320)
    args = parser.parse_args()
    result = create_fixture(
        args.output.resolve(),
        frame_count=args.frame_count,
        fps=args.fps,
        width=args.width,
        height=args.height,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
