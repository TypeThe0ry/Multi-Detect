"""Capture capture-only Gazebo 3-D annotations with exact sensor-time joins.

The output is an observation stream for a host-side binder.  It is not a
runtime input and it is not a training manifest.  RGB payload hashes and
Gazebo sensor timestamps are the only permitted join keys; host wall/monotonic
time is deliberately absent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any


def _stamp_ns(message: Any) -> int:
    stamp = getattr(getattr(message, "header", None), "stamp", None)
    if stamp is None:
        raise ValueError("annotation message has no header stamp")
    sec = int(getattr(stamp, "sec", 0))
    nsec = int(getattr(stamp, "nsec", 0))
    if sec < 0 or not 0 <= nsec < 1_000_000_000:
        raise ValueError("annotation message timestamp is invalid")
    return sec * 1_000_000_000 + nsec


def _header_data(message: Any) -> dict[str, str]:
    """Read protobuf HeaderDataMap values without stringifying repeated fields."""

    header = getattr(message, "header", None)
    result: dict[str, str] = {}
    for item in getattr(header, "data", ()):
        key = str(getattr(item, "key", ""))
        raw_values = getattr(item, "value", ())
        if isinstance(raw_values, str):
            values = [raw_values]
        else:
            values = [str(value) for value in raw_values]
        if not key or not values:
            continue
        if len(values) != 1:
            raise ValueError(f"annotation header value is ambiguous for key {key!r}")
        result[key] = values[0]
    return result


def _box_row(message: Any) -> dict[str, Any]:
    stamp_ns = _stamp_ns(message)
    header = _header_data(message)
    raw_sequence = header.get("seq")
    sequence = None
    if raw_sequence is not None:
        try:
            sequence = int(raw_sequence)
        except ValueError as exc:
            raise ValueError("annotation header seq is not an integer") from exc
        if sequence < 0:
            raise ValueError("annotation header seq cannot be negative")
    boxes: list[dict[str, Any]] = []
    for annotated in getattr(message, "annotated_box", ()):
        box = getattr(annotated, "box", None)
        center = getattr(box, "center", None)
        size = getattr(box, "boxSize", None)
        orientation = getattr(box, "orientation", None)
        if box is None or center is None or size is None or orientation is None:
            raise ValueError("annotation bounding-box message has incomplete geometry")
        boxes.append(
            {
                "label_id": int(getattr(annotated, "label", 0)),
                "center_xyz_m": [float(center.x), float(center.y), float(center.z)],
                "size_xyz_m": [float(size.x), float(size.y), float(size.z)],
                "orientation_xyzw": [
                    float(orientation.x),
                    float(orientation.y),
                    float(orientation.z),
                    float(orientation.w),
                ],
            }
        )
    return {
        "sequence": sequence,
        "sensor_stamp_ns": stamp_ns,
        "sensor_time_s": stamp_ns / 1_000_000_000.0,
        "captured_at_clock": "gazebo_sensor",
        "annotation_sensor_frame_id": header.get("frame_id"),
        "bounding_boxes_3d": boxes,
        "bounding_box_topic": "multidetect/annotation/bounding_boxes_3d",
        "instance_segmentation_topic": "multidetect/annotation/instance_segmentation",
        "instance_segmentation_labels_map_topic": (
            "multidetect/annotation/instance_segmentation/labels_map"
        ),
        "runtime_consumes_annotation_topics": False,
    }


def _segmentation_observation(message: Any, *, label_id: int = 42) -> tuple[dict[str, Any], bytes]:
    stamp_ns = _stamp_ns(message)
    width = int(getattr(message, "width", 0))
    height = int(getattr(message, "height", 0))
    step = int(getattr(message, "step", 0))
    pixel_format = int(getattr(message, "pixel_format", 0))
    payload = bytes(getattr(message, "data", b""))
    if width <= 0 or height <= 0 or step < width * 3:
        raise ValueError("labels_map geometry is invalid")
    if pixel_format != 3:  # gz.msgs.Image.RGB_INT8
        raise ValueError(f"labels_map pixel_format must be RGB_INT8(3), got {pixel_format}")
    if len(payload) != step * height:
        raise ValueError("labels_map payload length does not match step and height")
    min_x = width
    min_y = height
    max_x = -1
    max_y = -1
    visible_pixels = 0
    instance_ids: set[int] = set()
    for y in range(height):
        row_start = y * step
        for x in range(width):
            offset = row_start + x * 3
            # Panoptic RGB_INT8: semantic label is byte 2 and instance id is
            # byte 1 * 256 + byte 0.  This is not a BGR camera image.
            if payload[offset + 2] != label_id:
                continue
            visible_pixels += 1
            instance_ids.add(payload[offset + 1] * 256 + payload[offset])
            min_x = min(min_x, x)
            min_y = min(min_y, y)
            max_x = max(max_x, x)
            max_y = max(max_y, y)
    normalized_bbox = None
    if visible_pixels:
        normalized_bbox = [
            min_x / width,
            min_y / height,
            (max_x + 1) / width,
            (max_y + 1) / height,
        ]
    return (
        {
            "sensor_stamp_ns": stamp_ns,
            "sensor_time_s": stamp_ns / 1_000_000_000.0,
            "captured_at_clock": "gazebo_sensor",
            "width": width,
            "height": height,
            "step": step,
            "pixel_format": pixel_format,
            "encoding": "RGB_INT8",
            "payload_bytes": len(payload),
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "semantic_label_channel": 2,
            "instance_id_channels": [1, 0],
            "target_label_id": label_id,
            "visible_pixels": visible_pixels,
            "instance_ids": sorted(instance_ids),
            "visible_bbox_normalized": normalized_bbox,
        },
        payload,
    )


def capture(
    *,
    frames: int,
    timeout_seconds: float,
    bbox_topic: str,
    segmentation_topic: str,
    segmentation_output_dir: Path,
    rgb_hash_manifest: Path,
) -> list[dict[str, Any]]:
    if isinstance(frames, bool) or not isinstance(frames, int) or frames <= 0:
        raise ValueError("frames must be a positive integer")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    rgb_hash_manifest = rgb_hash_manifest.expanduser().resolve(strict=True)
    try:
        rgb_document = json.loads(rgb_hash_manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("RGB hash manifest is not valid JSON") from exc
    if (
        not isinstance(rgb_document, dict)
        or rgb_document.get("schema") != "multidetect.gazebo-annotation-sensor-staging"
        or rgb_document.get("training_ready") is not False
    ):
        raise ValueError("RGB hash manifest must be an offline sensor staging manifest")
    rgb_hashes: dict[int, tuple[int, str]] = {}
    for index, raw in enumerate(rgb_document.get("frames", [])):
        if not isinstance(raw, dict):
            raise ValueError(f"RGB hash manifest frame {index} is invalid")
        stamp = raw.get("sensor_stamp_ns")
        sequence = raw.get("sequence")
        digest = raw.get("rgb_payload_sha256")
        if (
            isinstance(stamp, bool)
            or not isinstance(stamp, int)
            or stamp < 0
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or digest.lower() != digest
        ):
            raise ValueError(f"RGB hash manifest frame {index} identity is invalid")
        if stamp in rgb_hashes:
            raise ValueError(f"duplicate RGB hash manifest timestamp: {stamp}")
        rgb_hashes[stamp] = (sequence, digest)
    if len(rgb_hashes) < frames:
        raise ValueError("RGB hash manifest does not cover the requested annotation frames")

    from gz.msgs10.annotated_oriented_3d_box_v_pb2 import AnnotatedOriented3DBox_V
    from gz.msgs10.image_pb2 import Image
    from gz.transport13 import Node

    segmentation_output_dir = segmentation_output_dir.expanduser().resolve()
    segmentation_output_dir.mkdir(parents=True, exist_ok=False)
    lock = threading.Lock()
    bbox_done = threading.Event()
    rows: dict[int, dict[str, Any]] = {}
    segmentations: dict[int, tuple[dict[str, Any], bytes]] = {}
    callback_errors: list[str] = []

    def on_bbox(message: Any) -> None:
        try:
            row = _box_row(message)
            stamp_ns = int(row["sensor_stamp_ns"])
            with lock:
                if stamp_ns in rows:
                    callback_errors.append(f"duplicate bounding-box timestamp {stamp_ns}")
                else:
                    rows[stamp_ns] = row
                if len(rows) >= frames:
                    bbox_done.set()
        except (AttributeError, TypeError, ValueError) as exc:
            callback_errors.append(f"annotation bounding-box rejected: {exc}")

    def on_segmentation(message: Any) -> None:
        try:
            observation, payload = _segmentation_observation(message)
            stamp_ns = int(observation["sensor_stamp_ns"])
            with lock:
                if stamp_ns in segmentations:
                    callback_errors.append(f"duplicate labels-map timestamp {stamp_ns}")
                else:
                    segmentations[stamp_ns] = (observation, payload)
        except (AttributeError, TypeError, ValueError) as exc:
            callback_errors.append(f"labels-map rejected: {exc}")

    node = Node()
    if not node.subscribe(AnnotatedOriented3DBox_V, bbox_topic, on_bbox):
        raise RuntimeError(f"failed to subscribe to {bbox_topic}")
    if not node.subscribe(Image, segmentation_topic, on_segmentation):
        node.unsubscribe(bbox_topic)
        raise RuntimeError(f"failed to subscribe to {segmentation_topic}")
    try:
        deadline = time.monotonic() + timeout_seconds
        if not bbox_done.wait(timeout_seconds):
            raise TimeoutError(
                f"timed out waiting for {frames} annotation rows; received {len(rows)}"
            )
        while True:
            with lock:
                selected = sorted(rows)[:frames]
                if len(selected) == frames and all(stamp in segmentations for stamp in selected):
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for exact labels-map timestamps")
            time.sleep(min(0.01, remaining))
        with lock:
            if callback_errors:
                raise ValueError("; ".join(callback_errors))
            result: list[dict[str, Any]] = []
            for stamp_ns in sorted(rows)[:frames]:
                row = dict(rows[stamp_ns])
                rgb_identity = rgb_hashes.get(stamp_ns)
                if rgb_identity is None:
                    raise ValueError(f"missing exact RGB hash for annotation timestamp {stamp_ns}")
                expected_sequence, rgb_hash = rgb_identity
                if row.get("sequence") != expected_sequence:
                    raise ValueError(
                        "annotation sequence does not match RGB staging at sensor "
                        f"timestamp {stamp_ns}"
                    )
                row["rgb_payload_sha256"] = rgb_hash
                observation, payload = segmentations[stamp_ns]
                filename = f"labels-map-{stamp_ns:020d}.rgb8"
                path = segmentation_output_dir / filename
                with path.open("xb") as stream:
                    stream.write(payload)
                observation = dict(observation)
                observation["payload_path"] = filename
                row["instance_segmentation"] = observation
                result.append(row)
            return result
    finally:
        if not node.unsubscribe(segmentation_topic):
            print(f"warning: failed to unsubscribe {segmentation_topic}", file=sys.stderr)
        if not node.unsubscribe(bbox_topic):
            print(f"warning: failed to unsubscribe {bbox_topic}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--bbox-topic", default="/multidetect/annotation/bounding_boxes_3d")
    parser.add_argument(
        "--segmentation-topic",
        default="/multidetect/annotation/instance_segmentation/labels_map",
    )
    parser.add_argument("--segmentation-output-dir", type=Path, required=True)
    parser.add_argument(
        "--rgb-hash-manifest",
        type=Path,
        required=True,
        help="offline sensor staging manifest providing exact sensor timestamp/RGB hash joins",
    )
    args = parser.parse_args()
    try:
        rows = capture(
            frames=args.frames,
            timeout_seconds=args.timeout_seconds,
            bbox_topic=args.bbox_topic,
            segmentation_topic=args.segmentation_topic,
            segmentation_output_dir=args.segmentation_output_dir,
            rgb_hash_manifest=args.rgb_hash_manifest,
        )
    except (RuntimeError, TimeoutError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    json.dump(
        {
            "schema": "multidetect.gazebo-3d-annotation-observation-stream",
            "schema_version": 2,
            "contract": "multidetect.gazebo-3d-annotation-observation-stream-v2",
            "capture_only": True,
            "runtime_consumes_annotation_topics": False,
            "truth_injected_into_policy": False,
            "join_key": ["sensor_stamp_ns", "rgb_payload_sha256"],
            "coordinate_frame": "rendering_view_x_right_y_up_z_backward",
            "labels_map_topic": args.segmentation_topic,
            "frames": rows,
        },
        sys.stdout,
        ensure_ascii=False,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
