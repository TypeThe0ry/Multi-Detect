"""Fail-closed companion wire record for a Gazebo metric-depth image.

The RGB stream remains the repository's frozen ``MDGZSIM3`` record.  When a
depth contract is enabled, the helper writes one ``MDGZDEP1`` record directly
after its matching RGB record.  This module deliberately has no Gazebo or
numpy dependency so both the container helper and host backend validate the
same bytes.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from typing import BinaryIO

DEPTH_WIRE_MAGIC = b"MDGZDEP1"
DEPTH_WIRE_VERSION = 1
DEPTH_WIRE_HEADER = struct.Struct("<8sHHQQQIIIII32s")
DEPTH_WIRE_HEADER_BYTES = DEPTH_WIRE_HEADER.size
DEPTH_WIRE_MAX_METADATA_BYTES = 64 * 1024
DEPTH_WIRE_MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
DEPTH_WIRE_MAX_DIMENSION = 4096
DEPTH_WIRE_MAX_SKEW_NS = 100_000_000
DEPTH_WIRE_ENCODINGS = frozenset({"FLOAT32_M", "UINT16_MM"})
DEPTH_WIRE_REGISTRATION = "registered_to_rgb"
DEPTH_WIRE_COORDINATE_SPACE = "rgb_pixel_aligned"


class GazeboDepthWireError(ValueError):
    """Raised when a companion record is malformed or not safely bound."""


@dataclass(frozen=True, slots=True)
class GazeboDepthWireFrame:
    sequence: int
    rgb_sensor_stamp_ns: int
    source_stamp_ns: int
    width: int
    height: int
    stride_bytes: int
    metadata: dict[str, object]
    payload: bytes
    record_sha256: str

    @property
    def encoding(self) -> str:
        return str(self.metadata["encoding"])

    @property
    def source_topic(self) -> str:
        return str(self.metadata["source_topic"])

    @property
    def calibration_id(self) -> str:
        return str(self.metadata["calibration_id"])

    @property
    def calibration_sha256(self) -> str:
        return str(self.metadata["calibration_sha256"])


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            raise GazeboDepthWireError(
                f"unexpected EOF while reading depth record ({len(chunks)}/{size})"
            )
        chunks.extend(chunk)
    return bytes(chunks)


def _sha256_text(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise GazeboDepthWireError(f"{name} must be a lowercase SHA-256")
    if any(character not in "0123456789abcdef" for character in value):
        raise GazeboDepthWireError(f"{name} must be a lowercase SHA-256")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GazeboDepthWireError(f"{name} must be a positive integer")
    return value


def _sequence(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GazeboDepthWireError("sequence must be a non-negative integer")
    return value


def _validate_metadata(metadata: Mapping[str, object]) -> dict[str, object]:
    required = {
        "sequence",
        "rgb_sensor_stamp_ns",
        "rgb_record_sha256",
        "source_stamp_ns",
        "source_topic",
        "calibration_id",
        "calibration_sha256",
        "registration_mode",
        "coordinate_space",
        "encoding",
        "distance_semantics",
        "width",
        "height",
        "stride_bytes",
        "minimum_depth_m",
        "maximum_depth_m",
        "fx_px",
        "fy_px",
        "cx_px",
        "cy_px",
    }
    unknown = set(metadata) - required
    if unknown or set(metadata) != required:
        raise GazeboDepthWireError(
            "depth metadata field set mismatch: "
            f"missing={sorted(required - set(metadata))}, unknown={sorted(unknown)}"
        )
    normalized = dict(metadata)
    sequence = _sequence(normalized["sequence"])
    rgb_stamp = _positive_int(normalized["rgb_sensor_stamp_ns"], "rgb_sensor_stamp_ns")
    source_stamp = _positive_int(normalized["source_stamp_ns"], "source_stamp_ns")
    if source_stamp > rgb_stamp or rgb_stamp - source_stamp > DEPTH_WIRE_MAX_SKEW_NS:
        raise GazeboDepthWireError("depth source timestamp is future or too stale")
    for name in ("source_topic", "calibration_id"):
        if not isinstance(normalized[name], str):
            raise GazeboDepthWireError(f"{name} is invalid")
        if name == "source_topic" and not normalized[name].startswith("/"):
            raise GazeboDepthWireError("source_topic is invalid")
    _sha256_text(normalized["rgb_record_sha256"], "rgb_record_sha256")
    _sha256_text(normalized["calibration_sha256"], "calibration_sha256")
    if normalized["registration_mode"] != DEPTH_WIRE_REGISTRATION:
        raise GazeboDepthWireError("depth is not explicitly registered to RGB")
    if normalized["coordinate_space"] != DEPTH_WIRE_COORDINATE_SPACE:
        raise GazeboDepthWireError("depth coordinate space is not RGB pixel aligned")
    if normalized["encoding"] not in DEPTH_WIRE_ENCODINGS:
        raise GazeboDepthWireError("unsupported depth encoding")
    if normalized["distance_semantics"] != "optical_axis_z_m":
        raise GazeboDepthWireError("Gazebo R_FLOAT32 depth must declare optical_axis_z_m")
    width = _positive_int(normalized["width"], "width")
    height = _positive_int(normalized["height"], "height")
    stride = _positive_int(normalized["stride_bytes"], "stride_bytes")
    if width > DEPTH_WIRE_MAX_DIMENSION or height > DEPTH_WIRE_MAX_DIMENSION:
        raise GazeboDepthWireError("depth geometry exceeds the safety bound")
    bytes_per_pixel = 4 if normalized["encoding"] == "FLOAT32_M" else 2
    if stride != width * bytes_per_pixel:
        raise GazeboDepthWireError("depth stride does not match encoding")
    for name in ("minimum_depth_m", "maximum_depth_m", "fx_px", "fy_px", "cx_px", "cy_px"):
        value = normalized[name]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise GazeboDepthWireError(f"{name} must be finite")
    if not 0.0 < float(normalized["minimum_depth_m"]) < float(normalized["maximum_depth_m"]):
        raise GazeboDepthWireError("depth range limits are invalid")
    if float(normalized["fx_px"]) <= 0.0 or float(normalized["fy_px"]) <= 0.0:
        raise GazeboDepthWireError("depth focal lengths must be positive")
    normalized["sequence"] = sequence
    normalized["rgb_sensor_stamp_ns"] = rgb_stamp
    normalized["source_stamp_ns"] = source_stamp
    normalized["width"] = width
    normalized["height"] = height
    normalized["stride_bytes"] = stride
    return normalized


def _canonical_metadata(metadata: Mapping[str, object]) -> bytes:
    normalized = _validate_metadata(metadata)
    try:
        encoded = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise GazeboDepthWireError("depth metadata is not JSON serializable") from exc
    if len(encoded) == 0 or len(encoded) > DEPTH_WIRE_MAX_METADATA_BYTES:
        raise GazeboDepthWireError("depth metadata exceeds the bounded size")
    return encoded


def build_depth_wire_record(
    *,
    metadata: Mapping[str, object],
    payload: bytes | bytearray | memoryview,
) -> bytes:
    """Build one complete, hash-bound MDGZDEP1 record."""

    normalized = _validate_metadata(metadata)
    metadata_bytes = _canonical_metadata(normalized)
    raw_payload = bytes(payload)
    expected_bytes = normalized["stride_bytes"] * normalized["height"]
    if len(raw_payload) != expected_bytes or len(raw_payload) > DEPTH_WIRE_MAX_PAYLOAD_BYTES:
        raise GazeboDepthWireError("depth payload geometry/size mismatch")
    header = DEPTH_WIRE_HEADER.pack(
        DEPTH_WIRE_MAGIC,
        DEPTH_WIRE_VERSION,
        DEPTH_WIRE_HEADER_BYTES,
        normalized["sequence"],
        normalized["rgb_sensor_stamp_ns"],
        normalized["source_stamp_ns"],
        normalized["width"],
        normalized["height"],
        normalized["stride_bytes"],
        len(raw_payload),
        len(metadata_bytes),
        b"\x00" * hashlib.sha256().digest_size,
    )
    digest = hashlib.sha256(header + metadata_bytes + raw_payload).digest()
    signed_header = header[:-32] + digest
    return signed_header + metadata_bytes + raw_payload


def _parse_record(header: bytes, metadata_bytes: bytes, payload: bytes) -> GazeboDepthWireFrame:
    (
        magic,
        version,
        header_bytes,
        sequence,
        rgb_stamp,
        source_stamp,
        width,
        height,
        stride,
        payload_bytes,
        metadata_size,
        digest,
    ) = DEPTH_WIRE_HEADER.unpack(header)
    if (
        magic != DEPTH_WIRE_MAGIC
        or version != DEPTH_WIRE_VERSION
        or header_bytes != DEPTH_WIRE_HEADER_BYTES
    ):
        raise GazeboDepthWireError("depth wire magic/version/header mismatch")
    if metadata_size != len(metadata_bytes) or payload_bytes != len(payload):
        raise GazeboDepthWireError("depth wire length mismatch")
    if metadata_size <= 0 or metadata_size > DEPTH_WIRE_MAX_METADATA_BYTES:
        raise GazeboDepthWireError("depth metadata size is outside the bound")
    if payload_bytes <= 0 or payload_bytes > DEPTH_WIRE_MAX_PAYLOAD_BYTES:
        raise GazeboDepthWireError("depth payload size is outside the bound")
    unsigned_header = header[:-32] + b"\x00" * 32
    if hashlib.sha256(unsigned_header + metadata_bytes + payload).digest() != digest:
        raise GazeboDepthWireError("depth record SHA-256 mismatch")
    try:
        raw_metadata = json.loads(metadata_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GazeboDepthWireError("depth metadata is not valid UTF-8 JSON") from exc
    if not isinstance(raw_metadata, dict):
        raise GazeboDepthWireError("depth metadata must be an object")
    metadata = _validate_metadata(raw_metadata)
    if (sequence, rgb_stamp, source_stamp) != (
        metadata["sequence"], metadata["rgb_sensor_stamp_ns"], metadata["source_stamp_ns"]
    ):
        raise GazeboDepthWireError("depth header/metadata causal fields mismatch")
    if (width, height, stride) != (metadata["width"], metadata["height"], metadata["stride_bytes"]):
        raise GazeboDepthWireError("depth header/metadata geometry mismatch")
    return GazeboDepthWireFrame(
        sequence=sequence,
        rgb_sensor_stamp_ns=rgb_stamp,
        source_stamp_ns=source_stamp,
        width=width,
        height=height,
        stride_bytes=stride,
        metadata=metadata,
        payload=payload,
        record_sha256=digest.hex(),
    )


def read_depth_wire_frame(
    stream: BinaryIO,
    *,
    expected_sequence: int,
    expected_rgb_sensor_stamp_ns: int,
    expected_rgb_record_sha256: str,
    expected_source_topic: str,
    expected_calibration_id: str,
    expected_calibration_sha256: str,
) -> GazeboDepthWireFrame:
    """Read one companion and bind it to the already accepted RGB record."""

    header = _read_exact(stream, DEPTH_WIRE_HEADER_BYTES)
    fields = DEPTH_WIRE_HEADER.unpack(header)
    metadata_size, payload_bytes = fields[10], fields[9]
    metadata = _read_exact(stream, metadata_size)
    payload = _read_exact(stream, payload_bytes)
    frame = _parse_record(header, metadata, payload)
    if frame.sequence != expected_sequence:
        raise GazeboDepthWireError("depth sequence does not match RGB")
    if frame.rgb_sensor_stamp_ns != expected_rgb_sensor_stamp_ns:
        raise GazeboDepthWireError("depth RGB timestamp does not match")
    if frame.metadata["rgb_record_sha256"] != expected_rgb_record_sha256:
        raise GazeboDepthWireError("depth RGB record digest does not match")
    if frame.source_topic != expected_source_topic:
        raise GazeboDepthWireError("depth source topic does not match binding")
    if frame.calibration_id != expected_calibration_id:
        raise GazeboDepthWireError("depth calibration ID does not match binding")
    if frame.calibration_sha256 != expected_calibration_sha256:
        raise GazeboDepthWireError("depth calibration digest does not match binding")
    return frame


def decode_depth_wire_blob(data: bytes, **kwargs: object) -> GazeboDepthWireFrame:
    """Test helper for decoding exactly one companion record."""

    stream = io.BytesIO(data)
    frame = read_depth_wire_frame(stream, **kwargs)  # type: ignore[arg-type]
    if stream.read(1):
        raise GazeboDepthWireError("depth wire has trailing bytes")
    return frame


__all__ = [
    "DEPTH_WIRE_COORDINATE_SPACE",
    "DEPTH_WIRE_ENCODINGS",
    "DEPTH_WIRE_HEADER",
    "DEPTH_WIRE_HEADER_BYTES",
    "DEPTH_WIRE_MAGIC",
    "DEPTH_WIRE_MAX_PAYLOAD_BYTES",
    "DEPTH_WIRE_MAX_SKEW_NS",
    "DEPTH_WIRE_REGISTRATION",
    "DEPTH_WIRE_VERSION",
    "GazeboDepthWireError",
    "GazeboDepthWireFrame",
    "build_depth_wire_record",
    "decode_depth_wire_blob",
    "read_depth_wire_frame",
]
