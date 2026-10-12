"""Lossless, timestamp-bound Gazebo panoptic labels-map companion records."""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from typing import Any, BinaryIO

HEADER = struct.Struct("<8sII32s")
MAGIC = b"MDGZSEG1"
MAX_PAYLOAD = 4096 * 4096 * 3


@dataclass(frozen=True, slots=True)
class SegmentationFrame:
    metadata: dict[str, Any]
    payload: bytes


def normalize_labels_map(message: Any) -> bytes:
    """Keep RGB_INT8 channels in their original order, stripping row padding."""
    width, height, step = int(message.width), int(message.height), int(message.step)
    if int(message.pixel_format_type) != 3:
        raise ValueError("segmentation labels_map must be RGB_INT8")
    if not 0 < width <= 4096 or not 0 < height <= 4096 or step < width * 3:
        raise ValueError("invalid segmentation geometry")
    data = bytes(message.data)
    if len(data) != step * height:
        raise ValueError("segmentation payload size mismatch")
    return b"".join(data[y * step : y * step + width * 3] for y in range(height))


def _validate(metadata: dict[str, Any], payload: bytes) -> None:
    if metadata.get("encoding") != "PANOPTIC_RGB8":
        raise ValueError("unsupported segmentation encoding")
    for key in ("width", "height"):
        value = metadata.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 4096:
            raise ValueError("invalid segmentation geometry")
    if len(payload) != metadata["width"] * metadata["height"] * 3:
        raise ValueError("segmentation payload size mismatch")
    for key in ("sequence", "source_stamp_ns", "rgb_sensor_stamp_ns"):
        value = metadata.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("invalid segmentation sequence or timestamp")
    digest = metadata.get("rgb_record_sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(
        char not in "0123456789abcdef" for char in digest
    ):
        raise ValueError("invalid segmentation RGB digest")
    topic = metadata.get("source_topic")
    if not isinstance(topic, str) or not topic.startswith("/"):
        raise ValueError("invalid segmentation source topic")
    if metadata.get("source_stamp_ns") != metadata.get("rgb_sensor_stamp_ns"):
        raise ValueError("segmentation must have the exact RGB simulation timestamp")


def build_segmentation_wire_record(metadata: dict[str, Any], payload: bytes) -> bytes:
    _validate(metadata, payload)
    encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > 65536:
        raise ValueError("segmentation metadata is too large")
    header = HEADER.pack(
        MAGIC, len(encoded), len(payload), hashlib.sha256(encoded + payload).digest()
    )
    return header + encoded + payload


def _exact(stream: BinaryIO, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        part = stream.read(length - len(data))
        if not part:
            raise ValueError("unexpected EOF reading segmentation companion")
        data.extend(part)
    return bytes(data)


def read_segmentation_wire_frame(
    stream: BinaryIO, *, sequence: int, stamp_ns: int, rgb_sha256: str,
    width: int, height: int, source_topic: str,
) -> SegmentationFrame:
    magic, metadata_size, payload_size, digest = HEADER.unpack(_exact(stream, HEADER.size))
    if magic != MAGIC or not 0 < metadata_size <= 65536 or not 0 < payload_size <= MAX_PAYLOAD:
        raise ValueError("invalid segmentation wire header")
    encoded, payload = _exact(stream, metadata_size), _exact(stream, payload_size)
    if hashlib.sha256(encoded + payload).digest() != digest:
        raise ValueError("segmentation record SHA-256 mismatch")
    metadata = json.loads(encoded)
    if not isinstance(metadata, dict):
        raise ValueError("invalid segmentation metadata")
    _validate(metadata, payload)
    expected = {"sequence": sequence, "rgb_sensor_stamp_ns": stamp_ns,
                "rgb_record_sha256": rgb_sha256, "width": width, "height": height,
                "source_topic": source_topic}
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise ValueError("segmentation companion does not match RGB binding")
    return SegmentationFrame(metadata, payload)
