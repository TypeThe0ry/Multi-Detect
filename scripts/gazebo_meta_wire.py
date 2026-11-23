"""Metadata-only wire record (``MDGZMET1``) for the SIM teacher frame server.

Answer to ``GETMETA <min_stamp_ns>``: the same synchronized telemetry that
``MDGZSIM3`` carries (stamp, camera-info stamp, pose, velocity) plus the
measured joint-state sidecar, with no RGB or depth pixels.

Layout (little endian)::

    magic        8 bytes  b"MDGZMET1"
    body_bytes   u32      length of the JSON body, 1..META_WIRE_MAX_BODY_BYTES
    body         utf-8 JSON object (sorted keys, compact separators)
    sha256       32 bytes SHA-256 over magic || body_bytes || body

It is only ever written in reply to ``GETMETA``; ``GET`` replies stay the frozen
``MDGZSIM3`` (+ ``MDGZDEP1``) records. No Gazebo or numpy dependency so the
container helper and the host reader share the same bytes.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from collections.abc import Mapping
from typing import Any, BinaryIO

META_WIRE_MAGIC = b"MDGZMET1"
META_WIRE_VERSION = 1
META_WIRE_MAX_BODY_BYTES = 256 * 1024
_LENGTH = struct.Struct("<I")
_DIGEST_BYTES = hashlib.sha256().digest_size

_REQUIRED_NUMBERS = (
    "sequence", "sensor_stamp_ns", "camera_info_stamp_ns", "navsat_stamp_ns",
    "odometry_stamp_ns", "latitude_deg", "longitude_deg", "altitude_m",
    "position_east_m", "position_north_m", "position_up_m",
    "orientation_x", "orientation_y", "orientation_z", "orientation_w",
    "velocity_forward_mps", "velocity_left_mps", "velocity_up_mps",
)


def build_meta_wire_record(body: Mapping[str, Any]) -> bytes:
    payload = json.dumps(
        {"version": META_WIRE_VERSION, **body}, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if not 0 < len(payload) <= META_WIRE_MAX_BODY_BYTES:
        raise ValueError("metadata body size is outside the supported range")
    prefix = META_WIRE_MAGIC + _LENGTH.pack(len(payload))
    return prefix + payload + hashlib.sha256(prefix + payload).digest()


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            raise ValueError(
                f"metadata stream ended early: expected {size} bytes, received {len(chunks)}"
            )
        chunks.extend(chunk)
    return bytes(chunks)


def read_meta_wire_frame(stream: BinaryIO, *, magic_consumed: bool = False) -> dict[str, Any]:
    """Read and verify one record. ``magic_consumed`` when the caller already peeked the 8 magic bytes."""
    if not magic_consumed and _read_exact(stream, len(META_WIRE_MAGIC)) != META_WIRE_MAGIC:
        raise ValueError("metadata wire magic mismatch")
    length_bytes = _read_exact(stream, _LENGTH.size)
    (length,) = _LENGTH.unpack(length_bytes)
    if not 0 < length <= META_WIRE_MAX_BODY_BYTES:
        raise ValueError("metadata body size is outside the supported range")
    payload = _read_exact(stream, length)
    digest = _read_exact(stream, _DIGEST_BYTES)
    if hashlib.sha256(META_WIRE_MAGIC + length_bytes + payload).digest() != digest:
        raise ValueError("metadata record SHA-256 mismatch")
    body = json.loads(payload.decode("utf-8"))
    if not isinstance(body, dict) or body.get("version") != META_WIRE_VERSION:
        raise ValueError("unsupported metadata record version")
    for key in _REQUIRED_NUMBERS:
        value = body.get(key)
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"metadata record field {key} is missing or not finite")
    if body["sensor_stamp_ns"] <= 0:
        raise ValueError("metadata sensor stamp must be positive")
    joint_state = body.get("joint_state")
    if joint_state is not None and not isinstance(joint_state, dict):
        raise ValueError("metadata joint_state must be an object or null")
    return body
