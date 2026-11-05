"""Recompute a narrow x500 SIM navigation-reference audit from original bytes.

No caller 'verified' flag, late diagnostic or stored sensor projection grants
qualification. This module is a permission-audit consumer, never an actor
observation source or actuator. It supports only the bound x500 model hierarchy
and measured zero IMU/GPS lever-arm parameters; unsupported offsets stay unknown.

Primary semantics: PX4 83c4f4e580558816f0e398918f66358988004696,
EKF2.cpp::PublishLocalPosition, GZBridge.cpp::clockCallback/navSatCallback,
streams/GPS_GLOBAL_ORIGIN.hpp and HOME_POSITION.hpp; gz-sim8
OdometryPublisher.cc::UpdateOdometry. Source identity is not authentication.
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .onboard_executed_trajectory import EvidenceBinding
from .serialization import (
    AutonomyContractError,
    canonical_json,
    canonical_sha256,
    require_int,
    require_real,
    require_text,
    strict_json_loads,
)
from .sim_gimbal_collision import load_gimbal_collision_geometry
from .sim_static_route_clearance import (
    ORIGIN_CONTRACT,
    _bound_bytes,
    _one,
    _pose,
    _read_bound_sdf,
    _vector,
)

if TYPE_CHECKING:
    from ..px4_external_mission import ExternalRuntimeAttestation
    from .contracts import ContextSnapshot

CONTRACT = "multidetect.SIM-navigation-reference-source-audit-v1"
CAPTURE_CONTRACT = "multidetect.teacher-SIM-origin-window-v1"
MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_CAPTURE_BYTES = 32 * 1024 * 1024
MAX_RGB_BYTES = 8 * 1024 * 1024
MAX_WINDOW_S = 2.0
MAX_SENSOR_SKEW_S = 0.15
MAX_RESIDUAL_M = 0.25
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_WIRE = struct.Struct("<8sHHIQIIIIQQQ22dI32s")
_PACKETS = {
    "LOCAL_POSITION_NED": (32, 185, "<Iffffff", ("time_boot_ms", "x", "y", "z", "vx", "vy", "vz")),
    "GLOBAL_POSITION_INT": (
        33,
        104,
        "<Iiiii3hH",
        ("time_boot_ms", "lat", "lon", "alt", "relative_alt", "vx", "vy", "vz", "hdg"),
    ),
    "GPS_GLOBAL_ORIGIN": (49, 39, "<iiiQ", ("latitude", "longitude", "altitude", "time_usec")),
    "HOME_POSITION": (
        242,
        104,
        "<iii10fQ",
        (
            "latitude",
            "longitude",
            "altitude",
            "x",
            "y",
            "z",
            "q1",
            "q2",
            "q3",
            "q4",
            "approach_x",
            "approach_y",
            "approach_z",
            "time_usec",
        ),
    ),
    "ATTITUDE": (
        30,
        39,
        "<I6f",
        ("time_boot_ms", "roll", "pitch", "yaw", "rollspeed", "pitchspeed", "yawspeed"),
    ),
    "ATTITUDE_QUATERNION": (
        31,
        246,
        "<I7f4f",
        (
            "time_boot_ms",
            "q1",
            "q2",
            "q3",
            "q4",
            "rollspeed",
            "pitchspeed",
            "yawspeed",
            "repr_offset_q1",
            "repr_offset_q2",
            "repr_offset_q3",
            "repr_offset_q4",
        ),
    ),
}
PARAMETERS = {
    "EKF2_IMU_POS_X": 0.0,
    "EKF2_IMU_POS_Y": 0.0,
    "EKF2_IMU_POS_Z": 0.0,
    "SENS_GPS0_OFFX": 0.0,
    "SENS_GPS0_OFFY": 0.0,
    "SENS_GPS0_OFFZ": 0.0,
    "SENS_GPS1_OFFX": 0.0,
    "SENS_GPS1_OFFY": 0.0,
    "SENS_GPS1_OFFZ": 0.0,
    "SENS_GPS0_ID": None,
    "SENS_GPS1_ID": None,
    "EKF2_HGT_REF": None,
    "SIM_GZ_EN_GPS": 1.0,
    "SIM_GZ_EN_ODOM": None,
    "EKF2_EV_CTRL": 0.0,
}


def _require(condition, message):
    if not condition:
        raise AutonomyContractError(message)


def _binding(root, raw, role="SIM-coordinate-audit-source", pointer=None):
    """Normalize existing native path/bytes/SHA records, never invent contents."""
    _require(isinstance(raw, Mapping), "original source binding required")
    path = Path(require_text(raw.get("path"), "source path"))
    absolute = path.resolve() if path.is_absolute() else (root / path).resolve()
    _require(absolute.is_relative_to(root), "source escapes artifact_root")
    return EvidenceBinding(
        absolute.relative_to(root).as_posix(),
        require_int(raw.get("bytes"), "source bytes", minimum=0),
        raw.get("sha256"),
        role,
        pointer,
    )


def _json_file(root, source, identities):
    raw = _bound_bytes(root, source, MAX_INPUT_BYTES, identities)
    _require(source.json_pointer in (None, ""), "whole original JSON source required")
    return strict_json_loads(raw)


def _crc(raw):
    value = 0xFFFF
    for byte in raw:
        temp = byte ^ (value & 0xFF)
        temp ^= (temp << 4) & 0xFF
        value = ((value >> 8) ^ (temp << 8) ^ (temp << 3) ^ (temp >> 4)) & 0xFFFF
    return value


def decode_navigation_packet(raw: bytes, kind: str) -> dict[str, object]:
    """Independent exact common-dialect CRC/payload decoder for six fixed types.

    MAVLink2 trailing-zero payload truncation is expanded only after checking
    the original CRC. Signed/unknown flags, foreign source, extra records and
    unsupported dialect types are rejected; CRC is integrity, not signature.
    """
    _require(
        type(raw) is bytes and 8 <= len(raw) <= 4096 and kind in _PACKETS,
        "bounded original supported navigation packet required",
    )
    message_id, extra, layout, names = _PACKETS[kind]
    if raw[0] == 0xFD:
        _require(
            len(raw) >= 12 and raw[2:4] == b"\x00\x00",
            "unsigned MAVLink2 with exact zero flags required",
        )
        header_size, length, sequence, system, component = 10, raw[1], raw[4], raw[5], raw[6]
        actual_id = int.from_bytes(raw[7:10], "little")
    elif raw[0] == 0xFE:
        header_size, length, sequence, system, component, actual_id = (
            6,
            raw[1],
            raw[2],
            raw[3],
            raw[4],
            raw[5],
        )
    else:
        raise AutonomyContractError("unsupported MAVLink navigation magic")
    size = struct.calcsize(layout)
    _require(
        actual_id == message_id and (system, component) == (1, 1),
        "navigation message/source differs",
    )
    _require(
        1 <= length <= size and len(raw) == header_size + length + 2,
        "navigation packet size/type differs",
    )
    if raw[0] == 0xFE:
        base_size = {"GPS_GLOBAL_ORIGIN": 12, "HOME_POSITION": 52, "ATTITUDE_QUATERNION": 32}.get(
            kind, size
        )
        _require(length == base_size, "MAVLink1 base payload size differs")
    _require(
        _crc(raw[1:-2] + bytes([extra])) == int.from_bytes(raw[-2:], "little"),
        "navigation packet CRC differs",
    )
    payload = raw[header_size:-2].ljust(size, b"\x00")
    values = dict(zip(names, struct.unpack(layout, payload), strict=True))
    # HOME surface quaternion may legally be unavailable; it is not used as
    # a current attitude. All coordinate/current-attitude fields must be finite.
    for name, value in values.items():
        if isinstance(value, float) and not name.startswith("q"):
            _require(math.isfinite(value), "navigation coordinate/velocity is nonfinite")
    return {
        **values,
        "message_type": kind,
        "packet_sequence": sequence,
        "source_system_id": system,
        "source_component_id": component,
    }


def parse_local_position_listener(raw: bytes) -> dict[str, object]:
    """Parse the actual uORB listener text, preserving missing reset/validity.

    Each required field occurs exactly once. Unused limit fields may be inf;
    coordinate/timestamp/reset/quality fields may not. Missing data is refused.
    """
    _require(
        type(raw) is bytes and 1 <= len(raw) <= 128 * 1024,
        "bounded listener original bytes required",
    )
    try:
        text = raw.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise AutonomyContractError("listener output is not exact ASCII") from exc
    _require(
        text.count("TOPIC: vehicle_local_position") == 1 and "groundtruth" not in text,
        "one actual vehicle_local_position listener sample required",
    )
    fields = {}
    for line in text.splitlines():
        match = re.fullmatch(r"[ \t]+([a-z][a-z0-9_]*):[ \t]+(.+?)[ \t]*", line)
        if match:
            _require(match[1] not in fields, "listener field is duplicated")
            fields[match[1]] = match[2]
    integer_names = (
        "timestamp",
        "timestamp_sample",
        "ref_timestamp",
        "xy_reset_counter",
        "z_reset_counter",
        "vxy_reset_counter",
        "vz_reset_counter",
        "heading_reset_counter",
    )
    float_names = (
        "x",
        "y",
        "z",
        "vx",
        "vy",
        "vz",
        "ref_lat",
        "ref_lon",
        "ref_alt",
        "eph",
        "epv",
        "heading",
    )
    boolean_names = (
        "xy_valid",
        "z_valid",
        "v_xy_valid",
        "v_z_valid",
        "xy_global",
        "z_global",
        "heading_good_for_control",
        "dead_reckoning",
    )
    result = {}
    for name in integer_names:
        value = fields.get(name, "")
        if name in {"timestamp", "timestamp_sample"}:
            value = value.split(" (")[0]
        _require(re.fullmatch(r"[0-9]+", value), f"listener {name} missing/invalid")
        result[name] = require_int(
            int(value),
            name,
            minimum=1 if "timestamp" in name else 0,
            maximum=2**64 - 1 if "timestamp" in name else 255,
        )
    for name in float_names:
        value = fields.get(name, "")
        _require(_NUMBER.fullmatch(value), f"listener {name} missing/nonfinite")
        result[name] = require_real(float(value), name)
    for name in boolean_names:
        value = fields.get(name)
        _require(value in ("True", "False"), f"listener {name} missing/invalid")
        result[name] = value == "True"
    _require(
        result["timestamp_sample"] <= result["timestamp"], "listener sample timestamp is future"
    )
    _require(
        result["ref_timestamp"] <= result["timestamp_sample"], "listener reference epoch is future"
    )
    return result


def _qrotate(q, point):
    x, y, z, w = q
    _require(abs(sum(v * v for v in q) - 1) <= 1e-3, "model quaternion is not normalized")
    return (
        (1 - 2 * (y * y + z * z)) * point[0]
        + 2 * (x * y - z * w) * point[1]
        + 2 * (x * z + y * w) * point[2],
        2 * (x * y + z * w) * point[0]
        + (1 - 2 * (x * x + z * z)) * point[1]
        + 2 * (y * z - x * w) * point[2],
        2 * (x * z - y * w) * point[0]
        + 2 * (y * z + x * w) * point[1]
        + (1 - 2 * (x * x + y * y)) * point[2],
    )


def _x500_model_offset(root, sources, identities):
    _require(
        isinstance(sources, Mapping) and set(sources) == {"x500_mono_cam", "x500", "x500_base"},
        "exact x500 composition/base model source closure required",
    )
    models = {}
    for name, binding in sources.items():
        document = _read_bound_sdf(root, binding, identities)
        _require(
            len(document) == 1
            and document[0].tag == "model"
            and document[0].attrib == {"name": name},
            "x500 model source identity differs",
        )
        models[name] = document[0]
    for name, uri in (("x500_mono_cam", "model://x500"), ("x500", "model://x500_base")):
        model = models[name]
        _require(model.find("pose") is None, "outer composition model pose override unsupported")
        matches = [item for item in model.findall("include") if _one(item, "uri").text == uri]
        _require(
            len(matches) == 1
            and matches[0].attrib == {"merge": "true"}
            and {child.tag for child in matches[0]} == {"uri"},
            "x500 merge include transform unsupported",
        )
    composite = models["x500_mono_cam"]
    odom = [
        p
        for p in composite.findall("plugin")
        if p.attrib.get("name") == "gz::sim::systems::OdometryPublisher"
    ]
    _require(
        len(odom) == 1
        and odom[0].attrib.get("filename") == "gz-sim-odometry-publisher-system"
        and _one(odom[0], "dimensions").text == "3"
        and all(child.tag in {"dimensions", "odom_publish_frequency"} for child in odom[0]),
        "model-world 3D odometry must have no custom frames/offset/noise",
    )
    base = models["x500_base"]
    rotation, offset = _pose(base)
    _require(
        rotation == ((1, 0, 0), (0, 1, 0), (0, 0, 1)) and offset == (0, 0, 0.24),
        "only original x500 .24 m model-to-base installation is supported",
    )
    links = [link for link in base.findall("link") if link.attrib.get("name") == "base_link"]
    _require(
        len(links) == 1 and _pose(links[0]) == (((1, 0, 0), (0, 1, 0), (0, 0, 1)), (0, 0, 0)),
        "base_link is not the supported body frame",
    )
    for name, kind in (("imu_sensor", "imu"), ("navsat_sensor", "navsat")):
        sensors = [
            sensor for sensor in links[0].findall("sensor") if sensor.attrib.get("name") == name
        ]
        _require(
            len(sensors) == 1
            and sensors[0].attrib == {"name": name, "type": kind}
            and _pose(sensors[0]) == (((1, 0, 0), (0, 1, 0), (0, 0, 1)), (0, 0, 0))
            and _one(sensors[0], "gz_frame_id").text == "base_link",
            "GPS/IMU sensor frame/lever arm unsupported",
        )
    return offset


@dataclass(frozen=True, slots=True)
class SimNavigationReferenceAudit:
    """A source-recomputed coordinate audit, not flight/resource authority."""

    _document_json: str

    def to_mapping(self) -> dict[str, object]:
        return strict_json_loads(self._document_json)

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @property
    def origin_qualified(self) -> bool:
        return self.to_mapping()["origin_qualified"]

    @property
    def derived_origin(self) -> dict[str, object] | None:
        return self.to_mapping()["derived_origin"]


def _native(root, row, arguments, capture, clock_id, identities):
    _require(
        isinstance(row, Mapping) and row.get("clock_id") == clock_id,
        "native observer clock differs",
    )
    command = row.get("command")
    _require(
        type(command) is list
        and len(command) == len(arguments) + 3
        and command[1:3] == ["exec", capture["session"]["container_id"]]
        and command[3:] == list(arguments)
        and row.get("container_id") == capture["session"]["container_id"],
        "native fixed readonly argv/container differs",
    )
    _require(
        row.get("native_exit_code") == 0
        and type(row.get("native_exit_code")) is int
        and row.get("API_attempted") is True
        and row.get("API_returned") is True
        and row.get("owned_process_exited") is True
        and row.get("reader_threads_exited") is True
        and row.get("complete_output_observed") is True
        and row.get("timeout") is False
        and row.get("error") is None,
        "native original output incomplete",
    )
    start = require_real(row.get("started_at_s"), "native start", minimum=0)
    end = require_real(row.get("finished_at_s"), "native end", minimum=start)
    stream = row["streams"]["stdout"]
    binding = _binding(root, stream["original_bytes"])
    _require(
        stream.get("all_bytes_retained") is True
        and stream.get("EOF") is True
        and stream.get("error") is None
        and stream.get("observed_bytes") == binding.bytes
        and stream.get("full_output_sha256") == binding.sha256,
        "native stdout prefix is not complete",
    )
    raw = _bound_bytes(root, binding, 256 * 1024, identities)
    return raw, binding, start, end


def _world(root, source, identities):
    sdf = _read_bound_sdf(root, source, identities)
    _require(len(sdf) == 1 and sdf[0].tag == "world", "one original world required")
    spherical = _one(sdf[0], "spherical_coordinates")
    _require(
        _one(spherical, "surface_model").text == "EARTH_WGS84"
        and _one(spherical, "world_frame_orientation").text == "ENU"
        and spherical.find("heading_deg") is None,
        "unrotated WGS84 world required",
    )
    lat = _vector(_one(spherical, "latitude_deg").text, 1, "world latitude")[0]
    lon = _vector(_one(spherical, "longitude_deg").text, 1, "world longitude")[0]
    alt = _vector(_one(spherical, "elevation").text, 1, "world elevation")[0]
    _require(
        -89 < lat < 89 and -180 <= lon <= 180 and abs(alt) < 10000,
        "world reference is outside local domain",
    )
    return {
        "latitude_deg": lat,
        "longitude_deg": lon,
        "elevation_m": alt,
        "altitude_datum": "SDF_world_elevation",
        "world_frame": "ENU",
        "heading_rad": 0,
    }


def _enu(reference, lat, lon, alt):
    """Exact WGS84 ECEF difference projected onto the bound world tangent axes."""

    def ecef(latitude, longitude, altitude):
        latitude, longitude = math.radians(latitude), math.radians(longitude)
        radius = 6378137.0 / math.sqrt(1 - 6.6943799901413165e-3 * math.sin(latitude) ** 2)
        return (
            (radius + altitude) * math.cos(latitude) * math.cos(longitude),
            (radius + altitude) * math.cos(latitude) * math.sin(longitude),
            (radius * (1 - 6.6943799901413165e-3) + altitude) * math.sin(latitude),
        )

    _require(
        -89 < lat < 89 and -180 <= lon <= 180 and abs(alt) < 10000,
        "navigation geographic coordinate out of range",
    )
    a = ecef(lat, lon, alt)
    b = ecef(reference["latitude_deg"], reference["longitude_deg"], reference["elevation_m"])
    x, y, z = (a[i] - b[i] for i in range(3))
    phi, lam = math.radians(reference["latitude_deg"]), math.radians(reference["longitude_deg"])
    return (
        -math.sin(lam) * x + math.cos(lam) * y,
        -math.sin(phi) * math.cos(lam) * x - math.sin(phi) * math.sin(lam) * y + math.cos(phi) * z,
        math.cos(phi) * math.cos(lam) * x + math.cos(phi) * math.sin(lam) * y + math.sin(phi) * z,
    )


def _camera(root, item, clock_id, identities):
    source = _binding(root, item["camera"], pointer="")
    document = _json_file(root, source, identities)
    stamp = document["host_return_stamp"]
    # Current producer uses MonotonicStamp.to_mapping; no UTC or reception
    # metadata can replace its original high-resolution host stamp.
    _require(stamp.get("session_id") == clock_id, "camera clock differs")
    host = require_int(stamp["value_ns"], "camera host timestamp", minimum=1) / 1e9
    _require(host == item["host_return_at_s"], "camera host projection differs")
    header = document["provenance"]["wire_unsigned_header_hex"]
    _require(
        type(header) is str and re.fullmatch(r"[0-9a-f]{552}", header),
        "exact unsigned camera wire header required",
    )
    raw = bytes.fromhex(header)
    values = _WIRE.unpack(raw)
    magic, version, size, sequence, sensor, width, height, stride, payload_size = values[:9]
    info, navsat, odometry = values[9:12]
    _require(
        (magic, version, size) == (b"MDGZSIM3", 3, _WIRE.size)
        and 1 <= width <= 4096
        and 1 <= height <= 4096
        and stride == width * 3
        and payload_size == stride * height
        and payload_size <= MAX_RGB_BYTES
        and values[-1] == bytes(32)
        and values[-2] == 65543,
        "supported complete RGB/NavSat/odometry wire required",
    )
    _require(
        sensor == item["source_sensor_stamp_ns"]
        and 0 < info <= sensor
        and 0 <= sensor - navsat <= MAX_SENSOR_SKEW_S * 1e9
        and 0 <= sensor - odometry <= MAX_SENSOR_SKEW_S * 1e9,
        "camera dynamic sensor skew/causality differs",
    )
    payload_source = _binding(root, document["persisted_payloads"]["rgb-bgr24"])
    payload = _bound_bytes(root, payload_source, MAX_RGB_BYTES, identities)
    _require(
        len(payload) == payload_size
        and hashlib.sha256(raw + payload).hexdigest() == document["provenance"]["record_sha256"]
        and payload_source.sha256 == document["rgb_bgr24_sha256"],
        "camera header/payload record identity differs",
    )
    numbers = values[12:34]
    _require(all(math.isfinite(v) for v in numbers), "camera wire coordinates nonfinite")
    return {
        "host_s": host,
        "sensor_s": sensor / 1e9,
        "odometry_s": odometry / 1e9,
        "navsat_s": navsat / 1e9,
        "latitude_deg": numbers[0],
        "longitude_deg": numbers[1],
        "altitude_m": numbers[2],
        "model_enu_m": numbers[3:6],
        "q_xyzw": numbers[6:10],
        "velocity_flu_mps": numbers[10:13],
        "sequence": sequence,
    }, source


def _journal_row(root, row, kind, capture, journals, identities):
    _require(
        row.get("source_accepted") is True
        and (row.get("system_id"), row.get("component_id")) == (1, 1),
        "foreign telemetry cannot be consumed for origin",
    )
    for field in ("clock_id", "transaction_id", "local_origin_id"):
        expected = capture["clock"]["session_id"] if field == "clock_id" else capture[field]
        _require(row.get(field) == expected, "telemetry identity differs")

    def record(stage):
        part = row[f"{stage}_record"]
        bound, raw = journals[stage]
        _require(
            _binding(root, {**part, "bytes": bound.bytes, "sha256": bound.sha256}).path
            == bound.path,
            "telemetry journal slice path differs",
        )
        offset = require_int(part["offset"], "journal offset", minimum=0)
        size = require_int(part["bytes"], "journal slice size", minimum=1, maximum=65536)
        data = raw[offset : offset + size]
        _require(
            offset + size <= len(raw) and hashlib.sha256(data).hexdigest() == part["sha256"],
            "journal slice bytes/hash differ",
        )
        return data

    raw_record = record("raw")
    _require(len(raw_record) >= 5, "raw journal record missing")
    header_size = int.from_bytes(raw_record[:4], "little")
    _require(
        0 < header_size <= 8192 and 4 + header_size < len(raw_record),
        "raw journal header size differs",
    )
    header = strict_json_loads(raw_record[4 : 4 + header_size])
    packet = raw_record[4 + header_size :]
    _require(
        header.get("contract") == "multidetect.onboard-teacher-raw-telemetry-v3"
        and header.get("record_index") == row["record_index"]
        and header.get("received_at_s") == row["received_at_s"]
        and header.get("packet_bytes") == len(packet)
        and header.get("packet_sha256") == row["packet_sha256"]
        and hashlib.sha256(packet).hexdigest() == row["packet_sha256"],
        "raw original packet/envelope identity differs",
    )
    projection = strict_json_loads(record("projection"))
    envelope = {
        k: v
        for k, v in row.items()
        if k
        not in {
            "raw_record",
            "projection_record",
            "raw_committed_at_s",
            "projection_committed_at_s",
            "dispatch_ready_at_s",
        }
    }
    _require(
        projection.get("encoding") == "multidetect.teacher-typed-values-v1"
        and projection.get("row") == envelope
        and projection.get("raw_record") == row["raw_record"],
        "journal projection differs from original row",
    )
    received = require_real(row["received_at_s"], "telemetry receive", minimum=0)
    raw_commit = require_real(row["raw_committed_at_s"], "raw commit", minimum=received)
    projection_commit = require_real(
        row["projection_committed_at_s"], "projection commit", minimum=raw_commit
    )
    require_real(row["dispatch_ready_at_s"], "dispatch ready", minimum=projection_commit)
    decoded = decode_navigation_packet(packet, kind)
    _require(
        row["packet_sequence"] == decoded["packet_sequence"]
        and row["message"]["message_type"] == kind,
        "decoded packet identity differs",
    )
    fields = _PACKETS[kind][3]
    for name in fields:
        if name.startswith(("q", "repr_offset", "approach_")):
            continue
        _require(
            name in row["message"] and row["message"][name] == decoded[name],
            "original decoded navigation projection differs",
        )
    return {**decoded, "received_at_s": received, "record_index": row["record_index"]}


def _snapshot_navigation(root, capture, decoded, identities):
    source = _binding(root, capture["snapshot_manifest"], pointer="")
    manifest = _json_file(root, source, identities)
    _require(
        manifest.get("rows") == capture.get("snapshot_rows")
        and manifest.get("received_times_renewed") is False,
        "original materialized snapshot differs or renews receive times",
    )
    for field in ("clock_id", "transaction_id", "local_origin_id"):
        expected = capture["clock"]["session_id"] if field == "clock_id" else capture[field]
        _require(manifest.get(field) == expected, "snapshot capture identity differs")
    result = {}
    for kind in ("HOME_POSITION", "LOCAL_POSITION_NED", "GLOBAL_POSITION_INT"):
        row = manifest["rows"][kind]
        packet = _bound_bytes(root, _binding(root, row["packet"]), 4096, identities)
        envelope = _json_file(root, _binding(root, row["envelope"], pointer=""), identities)
        _require(
            envelope
            == {
                k: v
                for k, v in row.items()
                if k not in {"packet", "envelope", "dispatch_ready_at_s"}
            },
            "snapshot original envelope differs",
        )
        independent = decode_navigation_packet(packet, kind)
        candidates = [r for r in decoded[kind] if r["record_index"] == row["record_index"]]
        _require(
            len(candidates) == 1
            and all(independent[k] == candidates[0][k] for k in independent)
            and hashlib.sha256(packet).hexdigest() == row["packet_sha256"]
            and candidates[0]["received_at_s"] == row["received_at_s"],
            "snapshot packet is not the specific original dual-committed row",
        )
        result[kind] = candidates[0]
    return result, source


def audit_sim_navigation_reference(
    *,
    artifact_root,
    capture_source: EvidenceBinding,
    world_source: EvidenceBinding,
    model_sources: Mapping[str, EvidenceBinding] | None = None,
    gimbal_sources: Mapping[str, EvidenceBinding] | None = None,
    observed_context: ContextSnapshot | None = None,
    runtime_attestation: ExternalRuntimeAttestation | None = None,
    now_s: float,
    clock_id: str,
) -> SimNavigationReferenceAudit:
    """Audit original co-window bytes; derive an origin only while inputs fresh.

    Invalid sources/unknown geometry/clock/reset mismatches raise fail-closed.
    A valid retained historical window returns an audit with no active origin.
    A derived origin is for the separate static-route auditor, not actor input,
    not loaded-world collision proof, and not Mission/ARM authority.
    """
    from ..px4_external_mission import ExternalRuntimeAttestation
    from .contracts import ContextSnapshot

    root, identities = Path(artifact_root).resolve(), {}
    now = require_real(now_s, "audit current time", minimum=0)
    active_inputs = observed_context is not None or runtime_attestation is not None
    if active_inputs:
        _require(
            type(observed_context) is ContextSnapshot
            and observed_context.schema_version == 2
            and type(runtime_attestation) is ExternalRuntimeAttestation
            and runtime_attestation.vehicle_class == "multirotor"
            and runtime_attestation.hardware_control_enabled is False
            and observed_context.airframe_profile_id == runtime_attestation.airframe_profile_id,
            "exact observed multirotor SIM context/runtime required",
        )
    _require(capture_source.json_pointer in (None, ""), "whole original capture required")
    capture = strict_json_loads(_bound_bytes(root, capture_source, MAX_CAPTURE_BYTES, identities))
    _require(
        capture.get("contract") == CAPTURE_CONTRACT
        and capture.get("status") == "origin-window-recorded"
        and capture.get("error") is None
        and capture.get("close_errors") == []
        and capture.get("clock", {}).get("session_id") == clock_id,
        "successful original co-window capture required; late probe cannot qualify",
    )
    docker = capture["plan"]["docker_plan"]
    identity = {
        "container_id": capture["session"]["container_id"],
        "image": docker["image"],
        "scenario_id": docker["scenario_id"],
        "scenario_manifest_sha256": docker["scenario_manifest_hash"],
        "airframe_profile_id": capture["profile"]["airframe_profile_id"],
    }
    _require(
        re.fullmatch(r"[0-9a-f]{64}", identity["container_id"])
        and re.fullmatch(r".+@sha256:[0-9a-f]{64}", identity["image"])
        and (
            identity["airframe_profile_id"] == "px4_multirotor_gazebo_v1"
            if gimbal_sources is None
            else identity["airframe_profile_id"] in {"x500_gimbal", "px4_x500_gimbal"}
        ),
        "bound capture SIM identity invalid",
    )
    if active_inputs:
        _require(
            all(getattr(runtime_attestation, key) == value for key, value in identity.items()),
            "capture runtime/image/scenario differs",
        )
    from ..simulator.stack import airframe_profile_hash

    profile_hash = airframe_profile_hash(capture["profile"])
    expected_model_name = "x500_mono_cam" if gimbal_sources is None else "x500_gimbal"
    _require(
        capture["profile"]["model_name"] == expected_model_name,
        "capture x500 profile differs",
    )
    if active_inputs:
        _require(
            profile_hash == observed_context.airframe_profile_hash
            and observed_context.provenance.runtime_kind == "SIM",
            "capture observed airframe profile differs",
        )
    reference = _world(root, world_source, identities)
    if gimbal_sources is None:
        _require(model_sources is not None, "fixed x500 model source closure required")
        offset = _x500_model_offset(root, model_sources, identities)
        sources = [capture_source, world_source, *model_sources.values()]
        gimbal_geometry = None
    else:
        _require(model_sources is None, "fixed x500 and gimbal model closures cannot be mixed")
        gimbal_geometry = load_gimbal_collision_geometry(root, gimbal_sources, identities)
        offset = gimbal_geometry.model_offset
        sources = [capture_source, world_source, *gimbal_geometry.source_bindings]
    runtime_source = _binding(root, capture["runtime_source"], pointer="")
    runtime = _json_file(root, runtime_source, identities)
    _require(
        runtime.get("scenario_id") == identity["scenario_id"]
        and runtime.get("vehicle", {}).get("airframe_profile_id")
        == identity["airframe_profile_id"],
        "source-bound scenario runtime differs",
    )
    sources.append(runtime_source)
    listeners = capture["listener_snapshots"]
    _require(
        type(listeners) is list
        and len(listeners) == 2
        and [r.get("role") for r in listeners] == ["before", "after"],
        "two ordered original listener snapshots required",
    )
    parsed, intervals = [], []
    for row in listeners:
        raw, binding, start, end = _native(
            root,
            row,
            ("/opt/px4-gazebo/bin/px4-listener", "vehicle_local_position", "-i", "0", "-n", "1"),
            capture,
            clock_id,
            identities,
        )
        parsed.append(parse_local_position_listener(raw))
        intervals.append((start, end))
        sources.append(binding)
    before, after = parsed
    _require(
        intervals[0][1] <= intervals[1][0] <= now
        and before["timestamp_sample"] < after["timestamp_sample"],
        "listener host/sender intervals regressed",
    )
    _require(
        intervals[1][1] - intervals[0][0] <= 15, "co-window bracket exceeds narrow audit budget"
    )
    stable = (
        "ref_timestamp",
        "ref_lat",
        "ref_lon",
        "ref_alt",
        "xy_reset_counter",
        "z_reset_counter",
        "vxy_reset_counter",
        "vz_reset_counter",
        "heading_reset_counter",
    )
    _require(all(before[k] == after[k] for k in stable), "EKF reference/reset continuity changed")
    _require(
        all(
            sample[k] is True
            for sample in parsed
            for k in ("xy_valid", "z_valid", "v_xy_valid", "v_z_valid", "xy_global", "z_global")
        )
        and all(
            sample["dead_reckoning"] is False
            and 0 <= sample["eph"] <= 2
            and 0 <= sample["epv"] <= 2
            for sample in parsed
        ),
        "EKF mapping validity/uncertainty failed",
    )
    # Ground magnetic alignment does not independently invalidate XYZ mapping.
    parameters = {}
    for name, expected in PARAMETERS.items():
        values = []
        for phase in ("before", "after"):
            row = capture[f"parameters_{phase}"][name]
            raw, binding, start, end = _native(
                root,
                row,
                ("/opt/px4-gazebo/bin/px4-param", "show", "-q", name),
                capture,
                clock_id,
                identities,
            )
            try:
                text = raw.decode("ascii").strip()
            except UnicodeDecodeError as exc:
                raise AutonomyContractError("parameter bytes are not ASCII") from exc
            _require(_NUMBER.fullmatch(text), "quiet parameter raw number invalid")
            value = require_real(float(text), "actual parameter")
            _require(
                value == row.get("actual_quiet_parameter_value"), "parameter projection differs"
            )
            _require(
                end <= intervals[0][0] if phase == "before" else start >= intervals[1][1],
                "parameter reads do not bracket listeners",
            )
            sources.append(binding)
            values.append(value)
        _require(
            values[0] == values[1] and (expected is None or values[0] == expected),
            "unsupported/changed IMU/GPS/EV parameter",
        )
        if expected is None:
            _require(
                values[0].is_integer() and 0 <= values[0] <= 2**32 - 1, "parameter enum/ID invalid"
            )
        if name == "SIM_GZ_EN_ODOM":
            _require(values[0] in (0, 1), "odometry transport flag unsupported")
        if name == "EKF2_HGT_REF":
            _require(values[0] in (0, 1, 2, 3), "height reference enum unsupported")
        parameters[name] = values[0]
    cameras = []
    _require(
        type(capture["camera_frames"]) is list and 1 <= len(capture["camera_frames"]) <= 4,
        "bounded original camera frames required",
    )
    for item in capture["camera_frames"]:
        frame, binding = _camera(root, item, clock_id, identities)
        _require(
            intervals[0][1] <= frame["host_s"] <= intervals[1][0]
            and before["timestamp_sample"] / 1e6 - MAX_SENSOR_SKEW_S
            <= frame["odometry_s"]
            <= after["timestamp_sample"] / 1e6 + MAX_SENSOR_SKEW_S,
            "camera not in original host/sender listener bracket",
        )
        cameras.append(frame)
        sources.append(binding)
    _require(
        all(
            a["sensor_s"] < b["sensor_s"] and a["host_s"] < b["host_s"]
            for a, b in zip(cameras, cameras[1:], strict=False)
        ),
        "camera epochs/order repeated or regressed",
    )
    sealed = capture["raw_telemetry"]
    _require(sealed["status"] == "recorded", "complete dual journal seal required")
    journals = {}
    for stage in ("raw", "projection"):
        binding = _binding(root, sealed["journal_sources"][stage])
        raw = _bound_bytes(root, binding, 64 * 1024 * 1024, identities)
        journals[stage] = (binding, raw)
        sources.append(binding)
    _require(journals["raw"][1].startswith(b"MDTEACHER-RAW-V3\n"), "raw journal version differs")
    decoded = {}
    for kind in _PACKETS:
        rows = capture["selected_telemetry"][kind]
        _require(type(rows) is list and len(rows) <= 16384, "bounded selected telemetry required")
        decoded[kind] = [
            _journal_row(root, row, kind, capture, journals, identities)
            for row in rows
            if row.get("source_accepted") is True
        ]
        _require(decoded[kind], f"actual accepted {kind} packet missing")
    listener_pairs = []
    for sample, interval in zip(parsed, intervals, strict=True):
        target_s = sample["timestamp_sample"] / 1e6
        candidates = [
            r
            for r in decoded["LOCAL_POSITION_NED"]
            if interval[0] - 0.15 <= r["received_at_s"] <= interval[1] + 0.15
        ]
        _require(candidates, "current listener has no original LOCAL packet near host interval")
        local = min(candidates, key=lambda r: abs(r["time_boot_ms"] / 1000 - target_s))
        skew = abs(local["time_boot_ms"] / 1000 - target_s)
        _require(skew <= MAX_SENSOR_SKEW_S, "listener/LOCAL sender units or sample age differs")
        speed = math.sqrt(sum(local[k] ** 2 for k in ("vx", "vy", "vz")))
        sample_speed = math.sqrt(sum(sample[k] ** 2 for k in ("vx", "vy", "vz")))
        _require(
            max(speed, sample_speed) <= 0.5, "navigation window is outside grounded motion scope"
        )
        delta = math.dist(
            tuple(local[k] for k in ("x", "y", "z")), tuple(sample[k] for k in ("x", "y", "z"))
        )
        _require(
            delta <= 0.02 + skew * max(speed, sample_speed),
            "actual listener/LOCAL navigation position differs",
        )
        listener_pairs.append(
            {
                "record_index": local["record_index"],
                "sender_skew_s": skew,
                "position_residual_m": delta,
            }
        )
    reference_scope = {}
    references = {}
    for kind, fields in (
        ("GPS_GLOBAL_ORIGIN", ("latitude", "longitude", "altitude", "time_usec")),
        ("HOME_POSITION", ("latitude", "longitude", "altitude", "x", "y", "z", "time_usec")),
    ):
        entries = decoded[kind]
        prior = [r for r in entries if r["received_at_s"] <= intervals[0][0]]
        within = [r for r in entries if intervals[0][0] <= r["received_at_s"] <= intervals[1][1]]
        _require(prior or within, "no original reference before/during listener window")
        anchor = max(prior, key=lambda r: r["received_at_s"]) if prior else within[0]
        _require(
            all(all(row[k] == anchor[k] for k in fields) for row in within),
            "HOME/global origin identity changed within window",
        )
        _require(
            within or kind == "GPS_GLOBAL_ORIGIN", "periodic HOME missing within listener window"
        )
        references[kind] = anchor
        changed_outside = [
            r for r in entries if r not in within and any(r[k] != anchor[k] for k in fields)
        ]
        reference_scope[kind] = {
            "anchor_record_index": anchor["record_index"],
            "anchor_received_at_s": anchor["received_at_s"],
            "window_records_checked": len(within),
            "out_of_window_identity_change_records": len(changed_outside),
            "out_of_window_changed_record_indices": [
                r["record_index"] for r in changed_outside[:256]
            ],
        }
    origin, home = references["GPS_GLOBAL_ORIGIN"], references["HOME_POSITION"]
    _require(
        origin["time_usec"] == before["ref_timestamp"]
        and abs(origin["latitude"] / 1e7 - before["ref_lat"]) <= 5.1e-7
        and abs(origin["longitude"] / 1e7 - before["ref_lon"]) <= 5.1e-7
        and abs(origin["altitude"] / 1000 - before["ref_alt"]) <= 0.0011
        and 0 < home["time_usec"] <= before["timestamp_sample"],
        "immutable GPS/HOME origin epoch/reference differs",
    )
    home_geo = _enu(
        reference, home["latitude"] / 1e7, home["longitude"] / 1e7, home["altitude"] / 1000
    )
    if active_inputs:
        _require(
            (
                observed_context.home_position.latitude_deg,
                observed_context.home_position.longitude_deg,
            )
            == (home["latitude"] / 1e7, home["longitude"] / 1e7),
            "observed HOME is not original packet HOME",
        )
    residuals, derived_homes, matched = [], [], []
    for frame in cameras:
        body_offset = _qrotate(frame["q_xyzw"], offset)
        body = tuple(frame["model_enu_m"][i] + body_offset[i] for i in range(3))
        navsat_enu = _enu(
            reference, frame["latitude_deg"], frame["longitude_deg"], frame["altitude_m"]
        )
        residuals.append(math.dist(body, navsat_enu))
        pair = {}
        for kind in (
            "LOCAL_POSITION_NED",
            "GLOBAL_POSITION_INT",
            "ATTITUDE",
            "ATTITUDE_QUATERNION",
        ):
            possible = [
                r for r in decoded[kind] if intervals[0][0] <= r["received_at_s"] <= intervals[1][1]
            ]
            _require(possible, "no original current navigation packet in listener window")
            row = min(possible, key=lambda r: abs(r["time_boot_ms"] / 1000 - frame["odometry_s"]))
            _require(
                abs(row["time_boot_ms"] / 1000 - frame["odometry_s"]) <= MAX_SENSOR_SKEW_S,
                "PX4 boot ms and Gazebo ns have no close direct epoch pair",
            )
            pair[kind] = row
        local, global_row = pair["LOCAL_POSITION_NED"], pair["GLOBAL_POSITION_INT"]
        projected = _enu(
            reference, global_row["lat"] / 1e7, global_row["lon"] / 1e7, global_row["alt"] / 1000
        )
        residuals.append(math.dist(body, projected))
        derived = (
            body[0] - (local["y"] - home["y"]),
            body[1] - (local["x"] - home["x"]),
            body[2] + (local["z"] - home["z"]),
        )
        derived_homes.append(derived)
        residuals.append(math.dist(derived, home_geo))
        _require(
            abs(
                global_row["alt"] / 1000
                - home["altitude"] / 1000
                - global_row["relative_alt"] / 1000
            )
            <= 0.1,
            "GLOBAL absolute/relative HOME altitude differs",
        )
        # PX4 attitude is FRD->NED; Gazebo is FLU->ENU. Compare physical UP
        # axis after both fixed basis changes, not quaternion component labels.
        attitude = pair["ATTITUDE_QUATERNION"]
        axis_ned = _qrotate(
            (attitude["q2"], attitude["q3"], attitude["q4"], attitude["q1"]), (0, 0, -1)
        )
        axis_enu = (axis_ned[1], axis_ned[0], -axis_ned[2])
        _require(
            math.dist(axis_enu, _qrotate(frame["q_xyzw"], (0, 0, 1))) <= 0.1,
            "actual PX4/model roll-pitch reference differs",
        )
        forward_ned = _qrotate(
            (attitude["q2"], attitude["q3"], attitude["q4"], attitude["q1"]), (1, 0, 0)
        )
        _require(
            math.dist(
                (forward_ned[1], forward_ned[0], -forward_ned[2]),
                _qrotate(frame["q_xyzw"], (1, 0, 0)),
            )
            <= 0.1,
            "actual PX4/model yaw reference differs",
        )
        matched.append(
            {
                "camera_sequence": frame["sequence"],
                "sensor_s": frame["sensor_s"],
                "navigation_body_enu_m": list(body),
                "home_enu_m": list(derived),
                "record_indices": {k: r["record_index"] for k, r in pair.items()},
            }
        )
    _require(
        max(residuals) <= MAX_RESIDUAL_M
        and max(math.dist(a, b) for a in derived_homes for b in derived_homes) <= MAX_RESIDUAL_M,
        "source-derived body/GPS/HOME coordinate residual exceeds narrow budget",
    )
    snapshot, snapshot_source = _snapshot_navigation(root, capture, decoded, identities)
    sources.append(snapshot_source)
    latest = snapshot["GLOBAL_POSITION_INT"]
    if active_inputs:
        _require(
            (
                observed_context.aircraft_position.latitude_deg,
                observed_context.aircraft_position.longitude_deg,
                observed_context.aircraft_position.altitude_m,
            )
            == (latest["lat"] / 1e7, latest["lon"] / 1e7, latest["relative_alt"] / 1000),
            "observed aircraft is not specific original snapshot GLOBAL packet",
        )
    # The original context timestamp and receive timestamps are retained.
    # Finalized historical captures prove a mapping window, not current authority.
    observed_at = observed_context.captured_at_s if active_inputs else None
    reference_changed_after = any(
        any(
            r["received_at_s"] > intervals[1][1]
            and r["received_at_s"] <= now
            and any(r[k] != references[kind][k] for k in fields)
            for r in decoded[kind]
        )
        for kind, fields in (
            ("GPS_GLOBAL_ORIGIN", ("latitude", "longitude", "altitude", "time_usec")),
            ("HOME_POSITION", ("latitude", "longitude", "altitude", "x", "y", "z", "time_usec")),
        )
    )
    fresh = (
        active_inputs
        and 0 <= now - observed_at < MAX_WINDOW_S
        and 0 <= now - intervals[1][1] < MAX_WINDOW_S
        and 0 <= now - latest["received_at_s"] < MAX_WINDOW_S
        and not reference_changed_after
    )
    uncertainty = (
        max(0.02, max(residuals), max(s["eph"] for s in parsed), max(s["epv"] for s in parsed))
        + 0.02
    )
    home_mean = [sum(p[i] for p in derived_homes) / len(derived_homes) for i in range(3)]
    unique_sources = list({s.path: s for s in sources}.values())
    _require(len(unique_sources) <= 64, "origin source closure exceeds consumer budget")
    derived_origin = None
    if fresh:
        derived_origin = {
            "contract": ORIGIN_CONTRACT,
            "schema_version": 1,
            "runtime_kind": "SIM",
            "vehicle_class": "multirotor",
            "observed_context_sha256": observed_context.canonical_hash,
            "airframe_profile_id": observed_context.airframe_profile_id,
            "airframe_profile_sha256": observed_context.airframe_profile_hash,
            "runtime_attestation_sha256": runtime_attestation.canonical_hash,
            "container_id": runtime_attestation.container_id,
            "image": runtime_attestation.image,
            "scenario_id": runtime_attestation.scenario_id,
            "scenario_manifest_sha256": runtime_attestation.scenario_manifest_sha256,
            "world_source": world_source.to_mapping(),
            "world_reference": reference,
            "reference_point": "PX4_navigation_reference_point",
            "verified_home_enu_m": home_mean,
            "home_position": observed_context.home_position.to_mapping(),
            "clock_id": clock_id,
            "observed_at_s": observed_at,
            "qualified_at_s": now,
            "expires_at_s": min(observed_at + 2, intervals[1][1] + 2, latest["received_at_s"] + 2),
            "horizontal_uncertainty_m": uncertainty,
            "vertical_uncertainty_m": uncertainty,
            "source_evidence": [s.to_mapping() for s in unique_sources],
            "verification": {
                "PX4_navigation_reference_point_identified": True,
                "Gazebo_world_ENU_pose_observed": True,
                "pose_to_navigation_reference_extrinsic_bound": True,
                "EKF_reset_continuity_verified": True,
                "WGS84_home_mapping_checked": True,
            },
        }
    gimbal_geometry_summary = None
    if gimbal_geometry is not None:
        gimbal_geometry_summary = {
            "collision_count": len(gimbal_geometry.collision_definitions),
            "source_expanded_collision_definitions": [
                collision.to_mapping()
                for _, collision in sorted(gimbal_geometry.collision_definitions.items())
            ],
            "source_joint_limits_rad": gimbal_geometry.joint_limits,
            "source_sphere_radius_m": gimbal_geometry.sphere_radius_m,
            "joint_range_swept_bounds": gimbal_geometry.swept_bounds(180.0),
        }
    # Reopen every referenced positive-length input before returning. Empty native
    # stderr has no semantic role and is not substituted for any required source.
    for path, old in identities.items():
        stat = (root / path).stat()
        _require(
            (stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino) == old,
            "audit source changed before return",
        )
    result = {
        "contract": CONTRACT,
        "runtime_kind": "SIM",
        "observed_context_sha256": observed_context.canonical_hash if active_inputs else None,
        "runtime_attestation_sha256": runtime_attestation.canonical_hash if active_inputs else None,
        "capture_identity": {**identity, "airframe_profile_sha256": profile_hash},
        "clock_id": clock_id,
        "audited_at_s": now,
        "origin_qualified": fresh,
        "derived_origin": derived_origin,
        "co_window_mapping_consistent": True,
        "window_coordinates_verified": True,
        "replay_verified": True,
        "current_inputs_fresh": fresh,
        "exact_synchronization_claimed": False,
        "blocking_gates": []
        if fresh
        else [
            "original-observation-or-listener-window-expired"
            if active_inputs
            else "missing-live-observed-context-and-runtime-admission"
        ],
        "model_to_navigation_body_offset_flu_m": list(offset),
        **(
            {"gimbal_collision_geometry": gimbal_geometry_summary}
            if gimbal_geometry_summary is not None
            else {}
        ),
        "actual_parameters": parameters,
        "reference": reference,
        "reference_scope": reference_scope,
        "listener_packet_pairs": listener_pairs,
        "reference_changed_after_window": reference_changed_after,
        "maximum_coordinate_residual_m": max(residuals),
        "matched_frames": matched,
        "source_evidence": [s.to_mapping() for s in unique_sources],
        "scope": (
            "source-integrity and narrow XYZ mapping; not source authentication, "
            "loaded-world collision coverage or flight acceptance"
        ),
        "claims": {
            "actor_truth_input": False,
            "flight_authority": False,
            "mission_uploaded": False,
            "ARM_sent": False,
            "training_completed": False,
            "model_promoted": False,
            "physical_performance_claimed": False,
            "HIL_REAL_evidence": False,
            "Jetson_evidence": False,
            "outdoor_GNSS_evidence": False,
            "final_release_allowed": False,
        },
    }
    return SimNavigationReferenceAudit(canonical_json(result))


def audit_sim_navigation_window(
    *,
    artifact_root,
    capture_source: EvidenceBinding,
    world_source: EvidenceBinding,
    model_sources: Mapping[str, EvidenceBinding],
    now_s: float,
    clock_id: str,
) -> SimNavigationReferenceAudit:
    """Pure original-byte replay audit; never invent live context/attestation."""
    return audit_sim_navigation_reference(
        artifact_root=artifact_root,
        capture_source=capture_source,
        world_source=world_source,
        model_sources=model_sources,
        now_s=now_s,
        clock_id=clock_id,
    )


__all__ = [
    "SimNavigationReferenceAudit",
    "audit_sim_navigation_reference",
    "audit_sim_navigation_window",
    "decode_navigation_packet",
    "parse_local_position_listener",
]
