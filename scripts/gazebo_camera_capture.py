"""Capture a bounded Gazebo Harmonic camera/telemetry sample.

This module runs inside the pinned ``px4io/px4-sitl-gazebo`` image.  It is
deliberately independent of the detector and of QGC: it proves that a real
Gazebo sensor topic produces timestamped pixels and avionics telemetry before
the transport/QGC integration gate is attempted.  Ground truth is never read
by this capture path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any


def _stamp_ns(message: Any) -> int:
    stamp = getattr(getattr(message, "header", None), "stamp", None)
    if stamp is None:
        return 0
    return int(stamp.sec) * 1_000_000_000 + int(stamp.nsec)


def _write_ppm(
    path: Path,
    *,
    data: bytes,
    width: int,
    height: int,
    step: int,
    channels: int,
) -> None:
    """Write one Gazebo image message as a portable RGB preview."""

    if channels not in {1, 3, 4}:
        raise ValueError(f"unsupported Gazebo pixel channel count: {channels}")
    minimum_row = width * channels
    if step < minimum_row or len(data) < step * height:
        raise ValueError("Gazebo image payload is shorter than its declared geometry")
    rgb = bytearray(width * height * 3)
    output_offset = 0
    for row_index in range(height):
        row = data[row_index * step : row_index * step + minimum_row]
        if channels == 1:
            for value in row:
                rgb[output_offset : output_offset + 3] = bytes((value, value, value))
                output_offset += 3
        elif channels == 3:
            rgb[output_offset : output_offset + width * 3] = row
            output_offset += width * 3
        else:
            for pixel_offset in range(0, minimum_row, 4):
                rgb[output_offset : output_offset + 3] = row[pixel_offset : pixel_offset + 3]
                output_offset += 3
    path.write_bytes(f"P6\n{width} {height}\n255\n".encode("ascii") + rgb)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=30)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-instance", default="x500_mono_cam_0")
    parser.add_argument("--world", default="default")
    parser.add_argument(
        "--persist-frames",
        action="store_true",
        help="persist every received image as a PPM for a downstream raw replay bridge",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if args.frames < 3:
        raise SystemExit("--frames must be at least 3 so first/middle/last previews exist")
    if args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds must be positive")

    # These imports only exist in the Gazebo runtime image.  Keeping them
    # inside main lets host-side syntax/contract checks run without Gazebo.
    from gz.msgs10.camera_info_pb2 import CameraInfo
    from gz.msgs10.image_pb2 import Image
    from gz.msgs10.navsat_pb2 import NavSat
    from gz.msgs10.odometry_with_covariance_pb2 import OdometryWithCovariance
    from gz.transport13 import Node

    model = args.model_instance
    image_topic = f"/world/{args.world}/model/{model}/link/camera_link/sensor/camera/image"
    camera_info_topic = (
        f"/world/{args.world}/model/{model}/link/camera_link/sensor/camera/camera_info"
    )
    navsat_topic = f"/world/{args.world}/model/{model}/link/base_link/sensor/navsat_sensor/navsat"
    odometry_topic = f"/model/{model}/odometry_with_covariance"
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    frames: list[dict[str, Any]] = []
    counters = {"navsat": 0, "odometry": 0, "camera_info": 0}
    latest: dict[str, Any] = {}
    image_error: list[str] = []

    def on_image(message: Image) -> None:
        with lock:
            if len(frames) >= args.frames or image_error:
                return
            channels = {1: 1, 2: 3, 3: 3, 4: 4}.get(int(message.pixel_format_type), 3)
            payload = bytes(message.data)
            frame_index = len(frames)
            digest = hashlib.sha256(payload).hexdigest()
            row = {
                "index": frame_index,
                "stamp_ns": _stamp_ns(message),
                "width": int(message.width),
                "height": int(message.height),
                "step": int(message.step),
                "pixel_format_type": int(message.pixel_format_type),
                "bytes": len(payload),
                "sha256": digest,
            }
            latest_navsat = latest.get("navsat")
            if isinstance(latest_navsat, dict):
                # The sample is the newest real NavSat message observed before
                # this image callback.  The bridge treats it as read-only
                # telemetry; it is never a detector answer or world truth.
                row["navsat"] = dict(latest_navsat)
            try:
                if args.persist_frames:
                    persisted_name = f"sensor-frame-{frame_index + 1:04d}.ppm"
                    _write_ppm(
                        output / persisted_name,
                        data=payload,
                        width=row["width"],
                        height=row["height"],
                        step=row["step"],
                        channels=channels,
                    )
                    row["persisted_path"] = persisted_name
                if frame_index in {0, args.frames // 2, args.frames - 1}:
                    _write_ppm(
                        output / f"frame-{frame_index + 1:04d}.ppm",
                        data=payload,
                        width=row["width"],
                        height=row["height"],
                        step=row["step"],
                        channels=channels,
                    )
            except (OSError, ValueError) as exc:
                image_error.append(str(exc))
                return
            frames.append(row)

    def on_navsat(message: NavSat) -> None:
        with lock:
            counters["navsat"] += 1
            latest["navsat"] = {
                "stamp_ns": _stamp_ns(message),
                "latitude_deg": float(message.latitude_deg),
                "longitude_deg": float(message.longitude_deg),
                "altitude_m": float(message.altitude),
            }

    def on_odometry(message: OdometryWithCovariance) -> None:
        with lock:
            counters["odometry"] += 1
            latest["odometry"] = {"stamp_ns": _stamp_ns(message)}

    def on_camera_info(message: CameraInfo) -> None:
        with lock:
            counters["camera_info"] += 1
            latest["camera_info"] = {
                "stamp_ns": _stamp_ns(message),
                "width": int(message.width),
                "height": int(message.height),
            }

    node = Node()
    subscriptions = [
        (Image, image_topic, on_image),
        (NavSat, navsat_topic, on_navsat),
        (OdometryWithCovariance, odometry_topic, on_odometry),
        (CameraInfo, camera_info_topic, on_camera_info),
    ]
    for message_type, topic, callback in subscriptions:
        if not node.subscribe(message_type, topic, callback):
            raise RuntimeError(f"Gazebo subscription failed: {topic}")

    deadline = time.monotonic() + args.timeout_seconds
    while time.monotonic() < deadline:
        with lock:
            complete = len(frames) >= args.frames
            failed = list(image_error)
        if complete or failed:
            break
        time.sleep(0.02)

    with lock:
        frame_rows = list(frames)
        count_snapshot = dict(counters)
        latest_snapshot = dict(latest)
        errors = list(image_error)
    stamps = [int(row["stamp_ns"]) for row in frame_rows]
    result = {
        "schema": "multidetect.gazebo-camera-capture",
        "schema_version": 1,
        "world": args.world,
        "model_instance": model,
        "image_topic": image_topic,
        "camera_info_topic": camera_info_topic,
        "navsat_topic": navsat_topic,
        "odometry_topic": odometry_topic,
        "requested_frames": args.frames,
        "persist_frames": args.persist_frames,
        "frame_count": len(frame_rows),
        "frames": frame_rows,
        "first_stamp_ns": stamps[0] if stamps else None,
        "last_stamp_ns": stamps[-1] if stamps else None,
        "strictly_increasing_timestamps": all(
            a < b for a, b in zip(stamps, stamps[1:], strict=False)
        ),
        "telemetry_message_counts": count_snapshot,
        "latest_telemetry": latest_snapshot,
        "preview_files": sorted(path.name for path in output.glob("frame-*.ppm")),
        "errors": errors,
        "gate_passed": (
            len(frame_rows) == args.frames
            and not errors
            and all(stamps[i] < stamps[i + 1] for i in range(len(stamps) - 1))
        ),
    }
    (output / "capture.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    event = {
        "event": "gazebo_camera_capture_finished",
        **{
            key: result[key]
            for key in ("gate_passed", "frame_count", "requested_frames", "image_topic")
        },
    }
    print(json.dumps(event, sort_keys=True), flush=True)
    return 0 if result["gate_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
