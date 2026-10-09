"""Live, main-writer SIM checkpoint admission; no flight or transport grant.

The V3 receiver/writer and retained origin-window consumer remain unchanged.
Growing journals are read only through their actual dual-committed prefix.
Original record indices and receive/sender epochs are never replaced. This
module can export an owned-writer checkpoint and consume source-bound native
observations; it does not launch Docker, receive MAVLink or send ARM/Mission.
"""

from __future__ import annotations

import copy
import hashlib
import math
import re
import struct
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from ..px4_external_mission import ExternalRuntimeAttestation, attest_owned_px4_container
from ..simulator.stack import airframe_profile_hash
from .contracts import ContextProvenance, ContextSnapshot, ContextValueSource, GeoPoint
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
from .sim_navigation_reference import (
    PARAMETERS,
    _binding,
    _camera,
    _crc,
    _enu,
    _journal_row,
    _qrotate,
    _require,
    _world,
    _x500_model_offset,
    decode_navigation_packet,
    parse_local_position_listener,
)
from .sim_static_route_clearance import (
    ORIGIN_CONTRACT,
    ExplicitSimOriginBinding,
    _bound_bytes,
)
from .sim_world_inputs import qualify_configured_sim_world_wind

CONTRACT = "multidetect.teacher-SIM-live-navigation-checkpoint-v1"
PREFIX_CONTRACT = "multidetect.teacher-live-dual-committed-prefix-v1"
MAXIMUM_AGE_S = 2.0
MAXIMUM_PREFIX_BYTES = 64 * 1024 * 1024
REQUIRED_MESSAGES = (
    "HEARTBEAT",
    "EXTENDED_SYS_STATE",
    "HOME_POSITION",
    "GPS_RAW_INT",
    "GLOBAL_POSITION_INT",
    "LOCAL_POSITION_NED",
    "BATTERY_STATUS",
    "SYS_STATUS",
    "VFR_HUD",
)
NAVIGATION_MESSAGES = (
    "HOME_POSITION",
    "GPS_GLOBAL_ORIGIN",
    "LOCAL_POSITION_NED",
    "GLOBAL_POSITION_INT",
    "ATTITUDE",
    "ATTITUDE_QUATERNION",
)
_ASCII_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_EXTRA_PACKETS = {
    "HEARTBEAT": (0, 50, 9, 9),
    "EXTENDED_SYS_STATE": (245, 130, 2, 2),
    "GPS_RAW_INT": (24, 24, 30, 52),
    "BATTERY_STATUS": (147, 154, 36, 54),
    "SYS_STATUS": (1, 124, 31, 43),
    "VFR_HUD": (74, 20, 20, 20),
}


def _save(root, path, raw, *, pointer=None):
    path = Path(path).resolve()
    _require(
        path.is_relative_to(root) and not path.exists(),
        "fresh contained checkpoint artifact required",
    )
    with path.open("xb") as stream:
        _require(stream.write(raw) == len(raw), "short checkpoint artifact write")
        stream.flush()
        import os

        os.fsync(stream.fileno())
    source = EvidenceBinding(
        path.relative_to(root).as_posix(),
        len(raw),
        hashlib.sha256(raw).hexdigest(),
        "live-checkpoint-original-source",
        pointer,
    )
    _bound_bytes(root, source, MAXIMUM_PREFIX_BYTES, {})
    return source


def _save_json(root, path, value):
    return _save(root, path, canonical_json(value).encode() + b"\n", pointer="")


def _configured_wind(root, world_source, runtime_source):
    def whole(source):
        _require(
            type(source) is EvidenceBinding and source.json_pointer in (None, ""),
            "whole-file configured wind inputs required",
        )
        value = source.to_mapping()
        value["json_pointer"] = None
        return EvidenceBinding.from_mapping(value)

    return qualify_configured_sim_world_wind(
        artifact_root=root, world_source=whole(world_source), runtime_source=whole(runtime_source)
    )


def capture_teacher_committed_prefix(capture, *, artifact_root, output_directory, clock):
    """Main-writer-only export while still open; never seal or restart RX.

    The actual frozen writer's ownership/reentrancy/commit state is checked.
    Only immutable selected packet/envelope copies and prefix ranges are saved;
    growing source journals are NOT declared immutable whole-file sources.
    """
    from scripts.onboard_teacher_telemetry_capture_v3 import TeacherTelemetryCaptureV3

    _require(
        isinstance(capture, TeacherTelemetryCaptureV3) and threading.get_ident() == capture._owner,
        "original main-owned V3 writer required",
    )
    capture._open()
    _require(
        not capture._busy
        and capture._clock == clock.monotonic
        and capture.clock_id == clock.session_id,
        "same live clock/idle writer required",
    )
    directory, root = Path(output_directory).resolve(), Path(artifact_root).resolve()
    _require(
        directory.is_relative_to(root) and not directory.exists(),
        "fresh contained prefix export required",
    )
    directory.mkdir()
    now = clock.monotonic()
    rows = capture.materialize_snapshot(list(REQUIRED_MESSAGES), directory / "snapshot", now_s=now)
    capture._open()
    prefixes = {}
    for stage in ("raw", "projection"):
        stream = capture._streams[stage]
        stat = Path(stream.name).stat()
        count = capture._commits[stage]
        _require(
            stat.st_size == capture._sizes[stage]
            and count["attempted"] == count["fsync_returned"] == count["verified"],
            "partial/unverified journal tail cannot qualify",
        )
        prefixes[stage] = {
            "path": Path(stream.name).as_posix(),
            "prefix_bytes": capture._sizes[stage],
            "prefix_sha256": capture._digests[stage].hexdigest(),
            "file_device": stat.st_dev,
            "file_inode": stat.st_ino,
            "append_allowed": True,
            "whole_file_immutable_claimed": False,
            "commits": copy.deepcopy(count),
        }
    # The writer's records are committed attempt/proof metadata, not message
    # objects. Recover their exact envelope from the verified projection slice;
    # do not manufacture it from a caller's decoder or the latest-only cache.
    selected = {kind: [] for kind in NAVIGATION_MESSAGES}
    for record in capture._records:
        if record["source_accepted"] is not True or record["message_type"] not in selected:
            continue
        projection = strict_json_loads(
            capture._read_slice("projection", record["projection_record"])
        )
        _require(
            projection["raw_record"] == record["raw_record"],
            "original navigation projection/raw proof differs",
        )
        selected[record["message_type"]].append(
            {
                **projection["row"],
                **{
                    k: copy.deepcopy(record[k])
                    for k in (
                        "raw_record",
                        "projection_record",
                        "raw_committed_at_s",
                        "projection_committed_at_s",
                    )
                },
                "dispatch_ready_at_s": record["projection_committed_at_s"],
            }
        )
    before = clock.monotonic()
    utc = clock.utc_now()
    after = clock.monotonic()
    _require(
        isinstance(utc, datetime) and utc.tzinfo is not None and 0 <= after - before <= 0.05,
        "actual bounded UTC/live-clock anchor required",
    )
    report = {
        "contract": PREFIX_CONTRACT,
        "writer_closed": False,
        "writer_live_at_export": True,
        "clock_id": capture.clock_id,
        "transaction_id": capture.transaction_id,
        "local_origin_id": capture.local_origin_id,
        "maximum_packets": capture.max_records,
        "record_count": len(capture._records),
        "exported_at_s": after,
        "committed_prefixes": prefixes,
        "snapshot_rows": rows,
        "selected_navigation_rows": selected,
        "snapshot_manifest": _binding(
            root,
            {
                "path": (directory / "snapshot/snapshot-manifest.json").as_posix(),
                "bytes": (directory / "snapshot/snapshot-manifest.json").stat().st_size,
                "sha256": hashlib.sha256(
                    (directory / "snapshot/snapshot-manifest.json").read_bytes()
                ).hexdigest(),
            },
            pointer="",
        ).to_mapping(),
        "UTC_anchor": {
            "before_monotonic_s": before,
            "actual_UTC": utc.isoformat(),
            "after_monotonic_s": after,
            "external_synchronization_claimed": False,
        },
        "claims": {"execution_authority": False, "source_authenticated": False, "sealed": False},
    }
    source = _save_json(root, directory / "committed-prefix.json", report)
    _read_prefixes(root, report)
    capture._open()
    return source


def _read_prefixes(root, document):
    _require(
        document["contract"] == PREFIX_CONTRACT
        and document["writer_closed"] is False
        and document["writer_live_at_export"] is True,
        "actual live prefix export required",
    )
    maximum = require_int(
        document["maximum_packets"], "actual instance packet limit", minimum=16, maximum=16384
    )
    require_int(document["record_count"], "committed records", minimum=9, maximum=maximum)
    result = {}
    for stage in ("raw", "projection"):
        prefix = document["committed_prefixes"][stage]
        original_path = Path(require_text(prefix["path"], "live journal path"))
        path = original_path.resolve()
        _require(
            path.is_relative_to(root) and path.is_file() and not original_path.is_symlink(),
            "contained original journal required",
        )
        length = require_int(
            prefix["prefix_bytes"],
            "committed prefix bytes",
            minimum=1,
            maximum=MAXIMUM_PREFIX_BYTES,
        )
        before = path.stat()
        _require(
            (before.st_dev, before.st_ino) == (prefix["file_device"], prefix["file_inode"])
            and before.st_size >= length
            and prefix["append_allowed"] is True
            and prefix["whole_file_immutable_claimed"] is False,
            "live prefix source identity differs",
        )
        with path.open("rb") as stream:
            raw = stream.read(length)
        after = path.stat()
        _require(
            (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
            and after.st_size >= before.st_size
            and len(raw) == length
            and hashlib.sha256(raw).hexdigest() == prefix["prefix_sha256"],
            "committed prefix changed/replaced/truncated",
        )
        counts = prefix["commits"]
        _require(
            all(type(v) is int and v >= 1 for v in counts.values())
            and counts["attempted"] == counts["fsync_returned"] == counts["verified"],
            "prefix contains unverified commits",
        )
        # Internal slice reader only. This is deliberately not emitted as a
        # whole-file EvidenceBinding: appending the next committed batch is legal.
        result[stage] = (
            SimpleNamespace(
                path=path.relative_to(root).as_posix(), bytes=length, sha256=prefix["prefix_sha256"]
            ),
            raw,
        )
    _require(result["raw"][1].startswith(b"MDTEACHER-RAW-V3\n"), "raw prefix version differs")
    return result


def _prefix_writer_identity(prefix, capture):
    capture._open()
    _require(
        prefix["maximum_packets"] == capture.max_records
        and prefix["clock_id"] == capture.clock_id
        and prefix["transaction_id"] == capture.transaction_id
        and prefix["local_origin_id"] == capture.local_origin_id,
        "prefix does not belong to exact current writer instance",
    )
    for stage in ("raw", "projection"):
        part = prefix["committed_prefixes"][stage]
        stream = capture._streams[stage]
        _require(
            not stream.closed
            and Path(part["path"]).resolve() == Path(stream.name).resolve()
            and (part["file_device"], part["file_inode"]) == capture._stats[stage][2:]
            and part["prefix_bytes"] <= capture._sizes[stage]
            and part["commits"]["verified"] <= capture._commits[stage]["verified"],
            "prefix original journal is not owned by current writer",
        )


def decode_context_packet(raw, kind):
    """Independently decode actual health/ground/battery fields and original CRC."""
    if kind not in _EXTRA_PACKETS:
        return decode_navigation_packet(raw, kind)
    message_id, extra, minimum, size = _EXTRA_PACKETS[kind]
    _require(type(raw) is bytes and len(raw) >= 8, "one actual context packet required")
    if raw[0] == 253:
        _require(len(raw) >= 12 and raw[2:4] == b"\0\0", "unsigned exact MAVLink2 required")
        header, length, sequence, system, component = 10, raw[1], raw[4], raw[5], raw[6]
        actual = int.from_bytes(raw[7:10], "little")
    elif raw[0] == 254:
        header, length, sequence, system, component, actual = (
            6,
            raw[1],
            raw[2],
            raw[3],
            raw[4],
            raw[5],
        )
        _require(length == minimum, "MAVLink1 base context length differs")
    else:
        raise AutonomyContractError("unsupported context wire magic")
    _require(
        actual == message_id
        and (system, component) == (1, 1)
        and 0 < length <= size
        and len(raw) == header + length + 2,
        "context packet type/source/length differs",
    )
    _require(
        _crc(raw[1:-2] + bytes([extra])) == int.from_bytes(raw[-2:], "little"),
        "context CRC differs",
    )
    payload = raw[header:-2].ljust(size, b"\0")
    result = {}
    if kind == "HEARTBEAT":
        names = "custom_mode type autopilot base_mode system_status mavlink_version".split()
        result = dict(zip(names, struct.unpack("<IBBBBB", payload), strict=True))
    elif kind == "EXTENDED_SYS_STATE":
        result = dict(zip(("vtol_state", "landed_state"), payload, strict=True))
    elif kind == "GPS_RAW_INT":
        names = (
            "time_usec lat lon alt eph epv vel cog fix_type satellites_visible "
            "alt_ellipsoid h_acc v_acc vel_acc hdg_acc yaw"
        ).split()
        result = dict(zip(names, struct.unpack("<QiiiHHHHBBiIIIIH", payload), strict=True))
    elif kind == "SYS_STATUS":
        names = (
            "onboard_control_sensors_present onboard_control_sensors_enabled "
            "onboard_control_sensors_health load voltage_battery current_battery "
            "drop_rate_comm errors_comm errors_count1 errors_count2 errors_count3 "
            "errors_count4 battery_remaining"
        ).split()
        result = dict(zip(names, struct.unpack("<IIIHHhHHHHHHb", payload[:31]), strict=True))
    elif kind == "VFR_HUD":
        result = dict(
            zip(
                "airspeed groundspeed alt climb heading throttle".split(),
                struct.unpack("<ffffhH", payload),
                strict=True,
            )
        )
    else:
        first = struct.unpack("<iih10HhBBBbiB4HBI", payload)
        result = dict(
            zip(("current_consumed", "energy_consumed", "temperature"), first[:3], strict=True)
        )
        result.update(
            voltages=list(first[3:13]),
            current_battery=first[13],
            id=first[14],
            battery_function=first[15],
            type=first[16],
            battery_remaining=first[17],
            time_remaining=first[18],
            charge_state=first[19],
            voltages_ext=list(first[20:24]),
            mode=first[24],
            fault_bitmask=first[25],
        )
    return {
        **result,
        "message_type": kind,
        "packet_sequence": sequence,
        "source_system_id": system,
        "source_component_id": component,
    }


def _snapshot_messages(root, prefix, journals, now, identities):
    rows = prefix["snapshot_rows"]
    _require(
        isinstance(rows, Mapping) and set(rows) == set(REQUIRED_MESSAGES),
        "exact nine original context rows required",
    )
    maximum = prefix["maximum_packets"]
    messages, times, sources = {}, {}, []
    for kind, row in rows.items():
        index = require_int(
            row["record_index"], "original record index", minimum=0, maximum=maximum - 1
        )
        _require(
            index < prefix["record_count"]
            and row["source_accepted"] is True
            and (row["system_id"], row["component_id"]) == (1, 1),
            "foreign/uncommitted context row",
        )
        for key, expected in (
            ("clock_id", prefix["clock_id"]),
            ("transaction_id", prefix["transaction_id"]),
            ("local_origin_id", prefix["local_origin_id"]),
        ):
            _require(row[key] == expected, "context source identity differs")
        at = require_real(row["received_at_s"], "original received time", minimum=0)
        _require(
            0 <= now - at < MAXIMUM_AGE_S and at <= row["dispatch_ready_at_s"] <= now,
            "original context packet stale/future/not ready",
        )
        packet_source = _binding(root, row["packet"])
        envelope_source = _binding(root, row["envelope"], pointer="")
        raw = _bound_bytes(root, packet_source, 4096, identities)
        envelope = strict_json_loads(_bound_bytes(root, envelope_source, 65536, identities))
        _require(
            envelope
            == {
                k: v
                for k, v in row.items()
                if k not in {"packet", "envelope", "dispatch_ready_at_s"}
            },
            "persisted original context envelope differs",
        )
        decoded = decode_context_packet(raw, kind)
        _require(
            decoded["packet_sequence"] == row["packet_sequence"]
            and hashlib.sha256(raw).hexdigest() == row["packet_sha256"],
            "context packet identity differs",
        )
        for key, value in decoded.items():
            if key in {
                "packet_sequence",
                "message_type",
                "source_system_id",
                "source_component_id",
            } or key.startswith(("q", "repr_offset")):
                continue
            projected = row["message"].get(key)
            if isinstance(value, float) and math.isnan(value):
                # Unavailable VFR airspeed remains unavailable, not repaired.
                _require(key == "airspeed", "nonfinite actual context measurement")
            else:
                _require(
                    projected == value, "original health/position projection differs from wire"
                )
        messages[kind], times[kind] = decoded, at
        sources.extend((packet_source, envelope_source))
    _require(
        len({r["record_index"] for r in rows.values()}) == 9
        and max(times.values()) - min(times.values()) <= 1,
        "context packet identities/skew differ",
    )
    # Independently prove that the immutable snapshot came from the stated
    # current dual-committed ranges, not caller-created health envelopes.
    manifest_source = EvidenceBinding.from_mapping(prefix["snapshot_manifest"])
    manifest = strict_json_loads(_bound_bytes(root, manifest_source, 1024 * 1024, identities))
    _require(
        manifest["rows"] == rows
        and manifest["received_times_renewed"] is False
        and all(
            manifest[k] == prefix[k] for k in ("clock_id", "transaction_id", "local_origin_id")
        ),
        "current snapshot manifest differs/renews times",
    )
    for kind, row in rows.items():
        proof = manifest["selected"][kind]
        raw_record = proof["raw_record"]
        original_path = _binding(
            root,
            {**raw_record, "bytes": journals["raw"][0].bytes, "sha256": journals["raw"][0].sha256},
        ).path
        _require(original_path == journals["raw"][0].path, "snapshot journal path differs")
        offset, size = raw_record["offset"], raw_record["bytes"]
        _require(
            type(offset) is int
            and type(size) is int
            and 0 <= offset
            and 0 < size <= 65536
            and offset + size <= journals["raw"][0].bytes,
            "snapshot slice is outside committed prefix",
        )
        segment = journals["raw"][1][offset : offset + size]
        _require(
            hashlib.sha256(segment).hexdigest() == raw_record["sha256"],
            "snapshot raw slice hash differs",
        )
        header_size = int.from_bytes(segment[:4], "little")
        header = strict_json_loads(segment[4 : 4 + header_size])
        packet = segment[4 + header_size :]
        _require(
            header["record_index"] == row["record_index"]
            and header["received_at_s"] == row["received_at_s"]
            and hashlib.sha256(packet).hexdigest() == row["packet_sha256"],
            "snapshot is not exact original committed packet",
        )
        projected = proof["projection_record"]
        _require(
            _binding(
                root,
                {
                    **projected,
                    "bytes": journals["projection"][0].bytes,
                    "sha256": journals["projection"][0].sha256,
                },
            ).path
            == journals["projection"][0].path,
            "projection journal path differs",
        )
        offset, size = projected["offset"], projected["bytes"]
        _require(
            type(offset) is int
            and type(size) is int
            and 0 <= offset
            and 0 < size <= 65536
            and offset + size <= journals["projection"][0].bytes,
            "projection outside committed prefix",
        )
        data = journals["projection"][1][offset : offset + size]
        _require(
            hashlib.sha256(data).hexdigest() == projected["sha256"],
            "snapshot projection slice differs",
        )
        original = strict_json_loads(data)
        _require(
            original["raw_record"] == raw_record
            and original["row"]
            == {
                k: v
                for k, v in row.items()
                if k not in {"packet", "envelope", "dispatch_ready_at_s"}
            },
            "snapshot projection is not original dual-committed envelope",
        )
    return messages, times, sources


def _ground_context(
    messages, times, profile, profile_source, resource_source, resource, anchor, now, transaction
):
    """Same grounded x500 v2 safety semantics, without legacy index renumbering."""
    heartbeat, extended, status = (
        messages[k] for k in ("HEARTBEAT", "EXTENDED_SYS_STATE", "SYS_STATUS")
    )
    _require(
        (heartbeat["type"], heartbeat["autopilot"], heartbeat["system_status"]) == (2, 12, 3)
        and not heartbeat["base_mode"] & 128
        and (extended["landed_state"], extended["vtol_state"]) == (1, 0),
        "actual PX4 x500 must be STANDBY/disarmed/on ground",
    )
    sensor_bits = 1 | 2 | 32 | 33554432
    _require(
        all(
            status[k] & sensor_bits == sensor_bits
            for k in (
                "onboard_control_sensors_present",
                "onboard_control_sensors_enabled",
                "onboard_control_sensors_health",
            )
        )
        and all(
            status[k] == 0
            for k in (
                "errors_comm",
                "errors_count1",
                "errors_count2",
                "errors_count3",
                "errors_count4",
            )
        ),
        "actual SYS_STATUS health/errors prevent context qualification",
    )
    gps, global_row, local, home, battery, vfr = (
        messages[k]
        for k in (
            "GPS_RAW_INT",
            "GLOBAL_POSITION_INT",
            "LOCAL_POSITION_NED",
            "HOME_POSITION",
            "BATTERY_STATUS",
            "VFR_HUD",
        )
    )
    _require(
        3 <= gps["fix_type"] <= 8
        and 6 <= gps["satellites_visible"] <= 254
        and 0 < gps["time_usec"]
        and 0 < gps["h_acc"] <= 2000,
        "actual GPS fix/accuracy is not qualified",
    )
    from ..geolocation_accuracy import wgs84_distance_m

    def point(row, lat="lat", lon="lon"):
        return GeoPoint(row[lat] / 1e7, row[lon] / 1e7)

    def distance(a, b):
        return wgs84_distance_m(
            latitude1_deg=a.latitude_deg,
            longitude1_deg=a.longitude_deg,
            latitude2_deg=b.latitude_deg,
            longitude2_deg=b.longitude_deg,
        )

    gps_point, global_point, home_point = (
        point(gps),
        point(global_row),
        point(home, "latitude", "longitude"),
    )
    _require(
        distance(gps_point, global_point) <= 2
        and distance(global_point, home_point) <= 2
        and abs(global_row["time_boot_ms"] - local["time_boot_ms"]) <= 500,
        "actual GPS/global/HOME or local/global timing disagreement",
    )
    relative_altitude = global_row["relative_alt"] / 1000
    _require(
        abs(relative_altitude) <= 0.5
        and abs(global_row["alt"] / 1000 - home["altitude"] / 1000 - relative_altitude) <= 1
        and abs(gps["alt"] - global_row["alt"]) / 1000 <= 3
        and math.hypot(local["x"] - home["x"], local["y"] - home["y"]) <= 2
        and abs(local["z"] - home["z"] + relative_altitude) <= 0.5,
        "actual altitude/NED/HOME does not qualify grounded state",
    )
    _require(
        math.sqrt(sum(local[k] ** 2 for k in ("vx", "vy", "vz"))) <= 0.3
        and math.sqrt(sum((global_row[k] / 100) ** 2 for k in ("vx", "vy", "vz"))) <= 0.3
        and 0 <= vfr["groundspeed"] <= 0.3,
        "actual vehicle motion is not grounded checkpoint scope",
    )
    raw_air = vfr["airspeed"]
    _require(
        math.isnan(raw_air) or math.isfinite(raw_air) and 0 <= raw_air <= 100,
        "actual airspeed has invalid sentinel/value",
    )
    _require(
        (
            battery["id"],
            battery["battery_function"],
            battery["charge_state"],
            battery["fault_bitmask"],
        )
        == (0, 1, 1, 0)
        and 1 <= battery["battery_remaining"] <= 100
        and 1 <= status["battery_remaining"] <= 100
        and abs(battery["battery_remaining"] - status["battery_remaining"]) <= 5
        and 0 <= battery["time_remaining"] <= 604800
        and len(battery["voltages"]) == 10
        and all(1 <= v <= 65535 for v in battery["voltages"])
        and battery["voltages"][0] != 65535
        and -1 <= battery["current_battery"] <= 32767,
        "actual primary battery health/data are not qualified",
    )
    endurance = (
        (battery["time_remaining"] - (now - times["BATTERY_STATUS"]))
        if (battery["time_remaining"] > 0 and battery["current_battery"] >= 0)
        else None
    )
    _require(endurance is None or endurance > 0, "actual reported endurance expired")
    oldest = min(*times.values(), resource["observed_at_s"])
    anchor_now = anchor["after_monotonic_s"]
    utc = datetime.fromisoformat(anchor["actual_UTC"].replace("Z", "+00:00"))
    _require(
        utc.tzinfo is not None
        and 0 <= anchor_now - anchor["before_monotonic_s"] <= 0.05
        and anchor_now <= now,
        "bounded actual UTC anchor differs",
    )
    return ContextSnapshot(
        schema_version=2,
        snapshot_id=f"{transaction}:live-qualified",
        captured_at_s=oldest,
        captured_at_utc=(utc - timedelta(seconds=anchor_now - oldest)).isoformat(),
        aircraft_position=GeoPoint(
            global_point.latitude_deg,
            global_point.longitude_deg,
            relative_altitude,
            "relative_home",
        ),
        home_position=home_point,
        airframe_profile_id=profile["airframe_profile_id"],
        airframe_profile_hash=airframe_profile_hash(profile),
        battery_remaining_pct=battery["battery_remaining"],
        estimated_flight_time_s=endurance,
        ground_speed_mps=vfr["groundspeed"],
        airspeed_mps=None,
        wind_north_mps=resource["wind_north_mps"],
        wind_east_mps=resource["wind_east_mps"],
        gps_fix_type=gps["fix_type"],
        gps_horizontal_accuracy_m=gps["h_acc"] / 1000,
        geofence_valid=False,
        camera_available=True,
        perception_available=False,
        map_cache_available=False,
        dem_available=False,
        provenance=ContextProvenance(
            "SIM",
            ContextValueSource("unknown"),
            ContextValueSource(
                "unknown" if endurance is None else "autopilot_reported",
                None if endurance is None else resource["battery_packet_sha256"],
            ),
            ContextValueSource("explicit_SIM_wind_model", resource_source.sha256),
        ),
    )


_PARAM_MARKER = "MD-LIVE-PARAMETERS-V1"
_UPDATE_BEFORE_MARKER = "MD-LIVE-PARAMETER-UPDATE-BEFORE-V1"
_UPDATE_MARKER = "MD-LIVE-PARAMETER-UPDATE-V1"
_LOCAL_MARKER = "MD-LIVE-LOCAL-POSITION-V1"
_END_MARKER = "MD-LIVE-NAV-END-V1"


def navigation_parameter_baseline_argv():
    """Fixed read-only one-exec command; values are actual px4-param output."""
    pieces = [
        "set -eu",
        f"printf '%s\\n' '{_UPDATE_BEFORE_MARKER}'",
        "/opt/px4-gazebo/bin/px4-listener parameter_update -i 0 -n 1",
        f"printf '%s\\n' '{_PARAM_MARKER}'",
    ]
    for name in PARAMETERS:
        pieces.extend((f"printf '%s=' '{name}'", f"/opt/px4-gazebo/bin/px4-param show -q {name}"))
    pieces.extend(
        (
            f"printf '%s\\n' '{_UPDATE_MARKER}'",
            "/opt/px4-gazebo/bin/px4-listener parameter_update -i 0 -n 1",
            f"printf '%s\\n' '{_END_MARKER}'",
        )
    )
    return ("/bin/sh", "-c", "; ".join(pieces))


def navigation_reference_phase_argv():
    """Fixed current reset/parameter-update read, no setter or vehicle command."""
    return (
        "/bin/sh",
        "-c",
        "; ".join(
            (
                "set -eu",
                f"printf '%s\\n' '{_UPDATE_MARKER}'",
                "/opt/px4-gazebo/bin/px4-listener parameter_update -i 0 -n 1",
                f"printf '%s\\n' '{_LOCAL_MARKER}'",
                "/opt/px4-gazebo/bin/px4-listener vehicle_local_position -i 0 -n 1",
                f"printf '%s\\n' '{_END_MARKER}'",
            )
        ),
    )


def _native_source(root, row, command, clock_id, now, identities):
    _require(
        isinstance(row, Mapping)
        and row["command"] == list(command)
        and row["clock_id"] == clock_id
        and type(row["native_exit_code"]) is int
        and row["native_exit_code"] == 0
        and row.get("API_attempted") is True
        and row.get("API_returned") is True
        and row["complete_output_observed"] is True
        and row["owned_process_exited"] is True
        and row["reader_threads_exited"] is True
        and row["timeout"] is False
        and row.get("error") is None,
        "actual complete fixed native readonly output required",
    )
    start = require_real(row["started_at_s"], "native source start", minimum=0)
    end = require_real(row["finished_at_s"], "native source finish", minimum=start, maximum=now)
    stream = row["streams"]["stdout"]
    source = _binding(root, stream["original_bytes"])
    _require(
        stream["all_bytes_retained"] is True
        and stream["EOF"] is True
        and stream.get("error") is None
        and stream["observed_bytes"] == source.bytes
        and stream["full_output_sha256"] == source.sha256,
        "native truncated stdout refused",
    )
    return _bound_bytes(root, source, 256 * 1024, identities), source, (start, end)


def _update_sample(text):
    _require(
        text.count("TOPIC: parameter_update") == 1, "one actual parameter_update sample required"
    )
    result = {}
    for key in ("timestamp", "instance", "set_count"):
        found = re.findall(rf"(?m)^[ \t]+{key}:[ \t]+([0-9]+)(?:[ \t]+\([^\n]*\))?[ \t]*$", text)
        _require(len(found) == 1, "parameter update identity missing/duplicated")
        result[key] = require_int(
            int(found[0]),
            key,
            minimum=1 if key == "timestamp" else 0,
            maximum=2**64 - 1 if key == "timestamp" else 2**32 - 1,
        )
    return result


def _native_navigation(root, inputs, prefix, plan, owner_token, now, identities):
    docker = plan["docker_plan"]
    container_id = inputs["container_id"]
    _require(re.fullmatch(r"[0-9a-f]{64}", container_id), "actual exact owned container required")
    executable = docker["docker_executable"]
    inspect_rows = []
    for row in (inputs["owned_inspect_before"], inputs["owned_inspect_after"]):
        raw, source, interval = _native_source(
            root,
            row,
            (executable, "container", "inspect", container_id),
            prefix["clock_id"],
            now,
            identities,
        )
        records = strict_json_loads(raw)
        _require(
            type(records) is list and len(records) == 1,
            "one actual Docker inspect original required",
        )
        record = records[0]
        labels = record["Config"]["Labels"]
        _require(
            record["Id"] == container_id
            and record["Name"] == "/" + docker["container_name"]
            and labels["multidetect.teacher-session-owner"] == owner_token
            and labels["multidetect.teacher-session-purpose"] == "truth-free-teacher-observation",
            "actual teacher owner identity changed",
        )
        inspect_rows.append((record, source, interval))
    baseline_raw, baseline_source, baseline_interval = _native_source(
        root,
        inputs["parameter_baseline"],
        (executable, "exec", container_id, *navigation_parameter_baseline_argv()),
        prefix["clock_id"],
        now,
        identities,
    )
    try:
        text = baseline_raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise AutonomyContractError("native navigation output must be ASCII") from exc
    _require(
        text.startswith(_UPDATE_BEFORE_MARKER + "\n")
        and text.endswith(_END_MARKER + "\n")
        and text.count(_UPDATE_MARKER) == 1
        and text.count(_PARAM_MARKER) == 1,
        "complete original parameter bundle required",
    )
    before_update_text, remainder = text[len(_UPDATE_BEFORE_MARKER) + 1 :].split(
        _PARAM_MARKER + "\n"
    )
    parameter_text, update_text = remainder.split(_UPDATE_MARKER + "\n")
    parameters = {}
    for line in parameter_text.splitlines():
        name, separator, value = line.partition("=")
        _require(
            separator
            and name in PARAMETERS
            and name not in parameters
            and _ASCII_NUMBER.fullmatch(value),
            "parameter bundle names/ASCII numbers differ",
        )
        parameters[name] = require_real(float(value), "actual baseline parameter")
    _require(set(parameters) == set(PARAMETERS), "actual 15-parameter baseline incomplete")
    for name, expected in PARAMETERS.items():
        _require(
            expected is None or parameters[name] == expected,
            "unsupported actual IMU/GPS/EV baseline",
        )
    _require(
        parameters["SIM_GZ_EN_ODOM"] in (0, 1)
        and parameters["EKF2_HGT_REF"] in (0, 1, 2, 3)
        and all(
            parameters[k].is_integer() and 0 <= parameters[k] <= 2**32 - 1
            for k in ("SENS_GPS0_ID", "SENS_GPS1_ID")
        ),
        "unsupported actual baseline enum/ID",
    )
    baseline_update = _update_sample(update_text.removesuffix(_END_MARKER + "\n"))
    _require(
        _update_sample(before_update_text) == baseline_update,
        "actual parameter baseline changed while values were read",
    )
    phases = []
    for row in (inputs["navigation_before"], inputs["navigation_after"]):
        raw, source, interval = _native_source(
            root,
            row,
            (executable, "exec", container_id, *navigation_reference_phase_argv()),
            prefix["clock_id"],
            now,
            identities,
        )
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise AutonomyContractError("phase native bytes are not ASCII") from exc
        _require(
            text.startswith(_UPDATE_MARKER + "\n")
            and text.endswith(_END_MARKER + "\n")
            and text.count(_LOCAL_MARKER) == 1,
            "complete actual navigation phase required",
        )
        update_text, local_text = text[len(_UPDATE_MARKER) + 1 :].split(_LOCAL_MARKER + "\n")
        update = _update_sample(update_text)
        _require(
            update == baseline_update, "actual parameter_update changed; re-read baseline required"
        )
        sample = parse_local_position_listener(
            local_text.removesuffix(_END_MARKER + "\n").encode("ascii")
        )
        phases.append((sample, source, interval))
    _require(
        baseline_interval[1] <= phases[0][2][0]
        and phases[0][2][1] <= phases[1][2][0]
        and inspect_rows[0][2][1] <= phases[0][2][0]
        and phases[1][2][1] <= inspect_rows[1][2][0],
        "owned inspect/baseline do not bracket actual navigation phase",
    )
    sources = [baseline_source, *(r[1] for r in phases), *(r[1] for r in inspect_rows)]
    return phases, inspect_rows, parameters, baseline_update, sources


def _navigation_geometry(
    root, prefix, journals, camera_updates, phases, world_source, models, now, identities, messages
):
    reference = _world(root, world_source, identities)
    installation = _x500_model_offset(root, models, identities)
    before, after = phases[0][0], phases[1][0]
    fields = (
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
    _require(
        all(before[k] == after[k] for k in fields)
        and before["timestamp_sample"] < after["timestamp_sample"],
        "actual EKF reference/reset changed inside live window",
    )
    _require(
        all(
            sample[k] is True
            for sample in (before, after)
            for k in ("xy_valid", "z_valid", "v_xy_valid", "v_z_valid", "xy_global", "z_global")
        )
        and all(
            not sample["dead_reckoning"] and 0 <= sample["eph"] < 1.9 and 0 <= sample["epv"] < 1.9
            for sample in (before, after)
        ),
        "actual navigation reference validity/uncertainty missing",
    )
    decoded = {}
    capture_shape = {
        "clock": {"session_id": prefix["clock_id"]},
        "transaction_id": prefix["transaction_id"],
        "local_origin_id": prefix["local_origin_id"],
    }
    for kind in NAVIGATION_MESSAGES:
        rows = prefix["selected_navigation_rows"][kind]
        _require(
            type(rows) is list and 1 <= len(rows) <= prefix["maximum_packets"],
            "actual navigation original set missing",
        )
        decoded[kind] = [
            _journal_row(root, row, kind, capture_shape, journals, identities) for row in rows
        ]

    def reference_packet(kind, names):
        entries = decoded[kind]
        prior = [r for r in entries if r["received_at_s"] <= phases[0][2][0]]
        within = [r for r in entries if phases[0][2][0] <= r["received_at_s"] <= phases[1][2][1]]
        _require(prior or within, "reference packet has no actual before/window origin")
        anchor = max(prior, key=lambda r: r["received_at_s"]) if prior else within[0]
        _require(
            all(all(r[k] == anchor[k] for k in names) for r in within)
            and all(
                not (r["received_at_s"] > phases[1][2][1] and any(r[k] != anchor[k] for k in names))
                for r in entries
            ),
            "actual HOME/reference changed inside or after live window",
        )
        return anchor

    home_keys = ("latitude", "longitude", "altitude", "x", "y", "z", "time_usec")
    home = reference_packet("HOME_POSITION", home_keys)
    origin = reference_packet(
        "GPS_GLOBAL_ORIGIN", ("latitude", "longitude", "altitude", "time_usec")
    )
    _require(
        all(messages["HOME_POSITION"][k] == home[k] for k in home_keys)
        and origin["time_usec"] == before["ref_timestamp"]
        and abs(origin["latitude"] / 1e7 - before["ref_lat"]) <= 5.1e-7
        and abs(origin["longitude"] / 1e7 - before["ref_lon"]) <= 5.1e-7
        and abs(origin["altitude"] / 1000 - before["ref_alt"]) <= 0.0011
        and 0 < home["time_usec"] <= before["timestamp_sample"],
        "actual HOME/reference packet does not match current EKF mapping",
    )
    for sample, _, interval in phases:
        candidates = [
            r
            for r in decoded["LOCAL_POSITION_NED"]
            if interval[0] - 0.15 <= r["received_at_s"] <= interval[1] + 0.15
        ]
        _require(candidates, "listener lacks original LOCAL close to actual host interval")
        pair = min(
            candidates,
            key=lambda r: abs(r["time_boot_ms"] / 1000 - sample["timestamp_sample"] / 1e6),
        )
        skew = abs(pair["time_boot_ms"] / 1000 - sample["timestamp_sample"] / 1e6)
        speed = max(math.sqrt(sum(r[k] ** 2 for k in ("vx", "vy", "vz"))) for r in (sample, pair))
        _require(
            skew <= 0.15
            and speed <= 0.3
            and math.dist(
                tuple(sample[k] for k in ("x", "y", "z")), tuple(pair[k] for k in ("x", "y", "z"))
            )
            <= 0.02 + skew * speed,
            "actual listener/LOCAL position/time disagreement",
        )
    _require(
        type(camera_updates) is list and 1 <= len(camera_updates) <= 2,
        "one or two actual current camera frames required",
    )
    source_list, residuals, homes, frame_times = [], [], [], []
    for item in camera_updates:
        frame, source = _camera(root, item, prefix["clock_id"], identities)
        _require(
            phases[0][2][1] <= frame["host_s"] <= phases[1][2][0]
            and 0 <= now - frame["host_s"] < 2
            and before["timestamp_sample"] / 1e6 - 0.15
            <= frame["odometry_s"]
            <= after["timestamp_sample"] / 1e6 + 0.15,
            "current original camera is not in actual listener host/sender window",
        )
        rotated = _qrotate(frame["q_xyzw"], installation)
        body = tuple(frame["model_enu_m"][i] + rotated[i] for i in range(3))
        actual_gps = _enu(
            reference, frame["latitude_deg"], frame["longitude_deg"], frame["altitude_m"]
        )
        residuals.append(math.dist(body, actual_gps))
        pairs = {}
        for kind in ("LOCAL_POSITION_NED", "GLOBAL_POSITION_INT", "ATTITUDE_QUATERNION"):
            candidates = [
                r for r in decoded[kind] if phases[0][2][0] <= r["received_at_s"] <= phases[1][2][1]
            ]
            _require(candidates, "current camera lacks original co-window navigation packet")
            pair = min(
                candidates, key=lambda r: abs(r["time_boot_ms"] / 1000 - frame["odometry_s"])
            )
            _require(
                abs(pair["time_boot_ms"] / 1000 - frame["odometry_s"]) <= 0.15,
                "current camera/navigation sender units/skew differ",
            )
            pairs[kind] = pair
        local, global_row, attitude = (
            pairs[k] for k in ("LOCAL_POSITION_NED", "GLOBAL_POSITION_INT", "ATTITUDE_QUATERNION")
        )
        projected = _enu(
            reference, global_row["lat"] / 1e7, global_row["lon"] / 1e7, global_row["alt"] / 1000
        )
        residuals.append(math.dist(body, projected))
        home_enu = (
            body[0] - local["y"] + home["y"],
            body[1] - local["x"] + home["x"],
            body[2] + local["z"] - home["z"],
        )
        residuals.append(
            math.dist(
                home_enu,
                _enu(
                    reference,
                    home["latitude"] / 1e7,
                    home["longitude"] / 1e7,
                    home["altitude"] / 1000,
                ),
            )
        )
        q = (attitude["q2"], attitude["q3"], attitude["q4"], attitude["q1"])
        for frd, flu in (((1, 0, 0), (1, 0, 0)), ((0, 0, -1), (0, 0, 1))):
            axis = _qrotate(q, frd)
            _require(
                math.dist((axis[1], axis[0], -axis[2]), _qrotate(frame["q_xyzw"], flu)) <= 0.1,
                "actual PX4 FRD/NED and model FLU/ENU attitudes differ",
            )
        source_list.append(source)
        frame_times.append(frame["host_s"])
        homes.append(home_enu)
    _require(
        max(residuals) <= 0.25 and max(math.dist(a, b) for a in homes for b in homes) <= 0.25,
        "actual live coordinate residual exceeds conservative budget",
    )
    token = {
        "ref_timestamp": before["ref_timestamp"],
        "origin_packet": {k: origin[k] for k in ("latitude", "longitude", "altitude", "time_usec")},
        "HOME": {k: home[k] for k in home_keys},
        "reset_counters": {k: before[k] for k in fields if k.endswith("reset_counter")},
        "world_source_sha256": world_source.sha256,
        "model_source_sha256": {k: v.sha256 for k, v in models.items()},
    }
    uncertainty = (
        max(
            messages["GPS_RAW_INT"]["h_acc"] / 1000,
            max(residuals),
            before["eph"],
            after["eph"],
            before["epv"],
            after["epv"],
        )
        + 0.02
        + 0.15 * 0.3
    )
    _require(uncertainty <= 2, "live origin uncertainty exceeds supported static-route domain")
    return {
        "home_enu_m": [sum(v[i] for v in homes) / len(homes) for i in range(3)],
        "reference": reference,
        "continuity_token": token,
        "uncertainty_m": uncertainty,
        "maximum_residual_m": max(residuals),
        "oldest_camera_at_s": min(frame_times),
    }, source_list


@dataclass(frozen=True, slots=True)
class LiveTeacherNavigationCheckpoint:
    """A source-qualified observation, never a Mission/transport/ARM grant."""

    _document_json: str
    checkpoint_source: EvidenceBinding
    _live_capture: object | None = None
    _live_clock: object | None = None

    def to_mapping(self):
        return strict_json_loads(self._document_json)

    @property
    def canonical_hash(self):
        return canonical_sha256(self.to_mapping())

    @property
    def observed_context(self):
        return ContextSnapshot.from_mapping(
            self.to_mapping()["observed_context_receipt"]["context"]
        )

    @property
    def observation_source(self):
        value = self.checkpoint_source.to_mapping()
        value["json_pointer"] = "/observed_context_receipt"
        return EvidenceBinding.from_mapping(value)

    @property
    def runtime_attestation(self):
        value = self.to_mapping()["runtime_attestation"]
        value.pop("attestation_sha256", None)
        return ExternalRuntimeAttestation.from_mapping(value)

    @property
    def clock_id(self):
        return self.to_mapping()["clock_id"]

    @property
    def qualified_at_s(self):
        return self.to_mapping()["qualified_at_s"]

    @property
    def expires_at_s(self):
        return self.to_mapping()["expires_at_s"]

    @property
    def inspected_at_s(self):
        return self.to_mapping()["inspected_at_s"]

    @property
    def origin(self):
        return ExplicitSimOriginBinding.from_mapping(self.to_mapping()["origin"])

    @property
    def origin_source(self):
        return EvidenceBinding.from_mapping(self.to_mapping()["origin_source"])

    @property
    def origin_continuity_token(self):
        return self.to_mapping()["origin_continuity_token"]

    @property
    def record_indices(self):
        return self.to_mapping()["record_indices"]

    @property
    def committed_prefixes(self):
        return self.to_mapping()["committed_prefixes"]

    @property
    def camera_source(self):
        return EvidenceBinding.from_mapping(self.to_mapping()["camera_source"])

    @property
    def world_source(self):
        return EvidenceBinding.from_mapping(self.to_mapping()["inputs"]["world_source"])

    @property
    def runtime_source(self):
        return EvidenceBinding.from_mapping(self.to_mapping()["inputs"]["runtime_source"])

    @property
    def model_sources(self):
        return {
            k: EvidenceBinding.from_mapping(v)
            for k, v in self.to_mapping()["inputs"]["model_sources"].items()
        }


def _recompute(root, inputs, input_source, now, clock_id):
    identities = {}

    def read(name):
        source = EvidenceBinding.from_mapping(inputs[name])
        raw = _bound_bytes(
            root, source, 32 * 1024 * 1024 if name == "prefix_source" else 128 * 1024, identities
        )
        return strict_json_loads(raw), source

    prefix, prefix_source = read("prefix_source")
    _require(prefix["clock_id"] == clock_id, "live checkpoint clock differs")
    journals = _read_prefixes(root, prefix)
    messages, times, message_sources = _snapshot_messages(root, prefix, journals, now, identities)
    profile, profile_source = read("profile_source")
    runtime, runtime_source = read("runtime_source")
    world_source = EvidenceBinding.from_mapping(inputs["world_source"])
    configured_wind = _configured_wind(root, world_source, runtime_source)
    models = {k: EvidenceBinding.from_mapping(v) for k, v in inputs["model_sources"].items()}
    plan = inputs["plan"]
    _require(
        profile["airframe_profile_id"] == "px4_multirotor_gazebo_v1"
        and profile["model_name"] == "x500_mono_cam"
        and profile["vehicle_model"] == "px4_multirotor_standard"
        and runtime["scenario_id"] == plan["docker_plan"]["scenario_id"]
        and runtime["vehicle"]["airframe_profile_id"] == profile["airframe_profile_id"],
        "actual source-bound x500 profile/runtime differs",
    )
    phases, inspects, parameters, parameter_update, native_sources = _native_navigation(
        root, inputs, prefix, plan, inputs["owner_token"], now, identities
    )
    geometry, camera_sources = _navigation_geometry(
        root,
        prefix,
        journals,
        inputs["camera_updates"],
        phases,
        world_source,
        models,
        now,
        identities,
        messages,
    )
    resource = strict_json_loads(
        _bound_bytes(
            root, EvidenceBinding.from_mapping(inputs["resource_source"]), 65536, identities
        )
    )
    _require(
        resource["clock_id"] == clock_id
        and resource["transaction_id"] == prefix["transaction_id"]
        and 0 <= now - resource["observed_at_s"] < 2
        and resource["airframe_profile_hash"] == airframe_profile_hash(profile)
        and resource["camera_available"] is True
        and all(
            resource[k] is False
            for k in (
                "geofence_valid",
                "perception_available",
                "map_cache_available",
                "dem_available",
            )
        )
        and resource["wind_source"] == "software-SIM-configured"
        and resource["wind_north_mps"] == configured_wind.wind_north_mps
        and resource["wind_east_mps"] == configured_wind.wind_east_mps
        and resource["configured_wind"] == configured_wind.to_mapping(),
        "actual limited camera/configured-wind resource source differs",
    )
    context = _ground_context(
        messages,
        times,
        profile,
        profile_source,
        EvidenceBinding.from_mapping(inputs["resource_source"]),
        resource,
        prefix["UTC_anchor"],
        now,
        prefix["transaction_id"],
    )
    docker = plan["docker_plan"]
    mounts = {v["container"]: v["host"] for v in docker["mounts"].values()}
    attestations = [
        attest_owned_px4_container(
            inspect[0],
            expected_container_id=inputs["container_id"],
            expected_image=docker["image"],
            vehicle_class="multirotor",
            scenario_id=docker["scenario_id"],
            scenario_manifest_sha256=docker["scenario_manifest_hash"],
            expected_model="gz_x500_mono_cam",
            expected_purpose_label="simulator-docker",
            control_port=docker["ports"]["control"]["host"],
            telemetry_qualified=True,
            expected_readonly_mounts=mounts,
        )
        for inspect in inspects
    ]
    _require(attestations[0] == attestations[1], "owned runtime changed inside current window")
    attestation = attestations[-1]
    oldest = min(
        context.captured_at_s, geometry["oldest_camera_at_s"], phases[-1][2][0], inspects[-1][2][0]
    )
    expires = oldest + 2
    _require(now < expires, "original live context/camera/native evidence expired")
    sources = [
        *message_sources,
        profile_source,
        EvidenceBinding.from_mapping(inputs["resource_source"]),
    ]
    receipt = {
        "contract": "multidetect.onboard-teacher-observed-context-v2",
        "schema_version": 2,
        "context": context.to_mapping(),
        "context_sha256": context.canonical_hash,
        "source_references": [s.to_mapping() for s in sources],
        "qualification_details": {
            "source_file_bytes_SHA_verified": True,
            "source_identity_before_after_equal": True,
            "qualified_plain_MC_disarmed_on_ground": True,
            "clock_id": clock_id,
            "accepted_system_component": [1, 1],
            "message_received_at_s": times,
            "resource_observed_at_s": resource["observed_at_s"],
            "oldest_observation_at_s": context.captured_at_s,
            "endurance_reserve_already_deducted": False,
            "maximum_packets": prefix["maximum_packets"],
            "original_record_indices_preserved": True,
            "unknown_values_fabricated": False,
            "UTC_anchor": prefix["UTC_anchor"],
            "bridge": "NEW-live-committed-prefix-v1",
        },
        "claims": {
            "execution_authority": False,
            "hardware_control_enabled": False,
            "mission_approved": False,
            "source_authenticated": False,
            "physical_performance_claimed": False,
        },
    }
    origin_sources = [
        input_source,
        *native_sources,
        *camera_sources,
        *message_sources,
        profile_source,
        runtime_source,
        world_source,
        *models.values(),
    ]
    by_path = {s.path: s for s in origin_sources}
    unique = [by_path[path] for path in sorted(by_path)]
    _require(
        len(unique) <= 64 and all(s.bytes <= 128 * 1024 for s in unique),
        "origin static consumer source bounds exceeded",
    )
    origin = {
        "contract": ORIGIN_CONTRACT,
        "schema_version": 1,
        "runtime_kind": "SIM",
        "vehicle_class": "multirotor",
        "observed_context_sha256": context.canonical_hash,
        "airframe_profile_id": context.airframe_profile_id,
        "airframe_profile_sha256": context.airframe_profile_hash,
        "runtime_attestation_sha256": attestation.canonical_hash,
        "container_id": attestation.container_id,
        "image": attestation.image,
        "scenario_id": attestation.scenario_id,
        "scenario_manifest_sha256": attestation.scenario_manifest_sha256,
        "world_source": world_source.to_mapping(),
        "world_reference": geometry["reference"],
        "reference_point": "PX4_navigation_reference_point",
        "verified_home_enu_m": geometry["home_enu_m"],
        "home_position": context.home_position.to_mapping(),
        "clock_id": clock_id,
        "observed_at_s": context.captured_at_s,
        "qualified_at_s": now,
        "expires_at_s": expires,
        "horizontal_uncertainty_m": geometry["uncertainty_m"],
        "vertical_uncertainty_m": geometry["uncertainty_m"],
        "source_evidence": [s.to_mapping() for s in unique],
        "verification": {
            "PX4_navigation_reference_point_identified": True,
            "Gazebo_world_ENU_pose_observed": True,
            "pose_to_navigation_reference_extrinsic_bound": True,
            "EKF_reset_continuity_verified": True,
            "WGS84_home_mapping_checked": True,
        },
    }
    ExplicitSimOriginBinding.from_mapping(origin)
    _read_prefixes(root, prefix)
    for path, identity in identities.items():
        stat = (root / path).stat()
        _require(
            (stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino) == identity,
            "immutable checkpoint source changed during revalidation",
        )
    return {
        "observed_context_receipt": receipt,
        "runtime_attestation": attestation.to_mapping(),
        "origin": origin,
        "origin_continuity_token": geometry["continuity_token"],
        "inspected_at_s": inspects[-1][2][0],
        "expires_at_s": expires,
        "record_indices": {k: r["record_index"] for k, r in prefix["snapshot_rows"].items()},
        "committed_prefixes": prefix["committed_prefixes"],
        "camera_source": camera_sources[-1].to_mapping(),
        "native_parameters": parameters,
        "parameter_update": parameter_update,
        "maximum_coordinate_residual_m": geometry["maximum_residual_m"],
        "configured_wind": configured_wind.to_mapping(),
    }


def qualify_teacher_live_navigation_checkpoint(
    *,
    artifact_root,
    output_directory,
    prefix_source,
    profile_source,
    runtime_source,
    world_source,
    model_sources,
    camera_updates,
    parameter_baseline,
    navigation_before,
    navigation_after,
    owned_inspect_before,
    owned_inspect_after,
    plan,
    container_id,
    owner_token,
    now_s,
    clock_id,
    live_capture,
    live_clock,
):
    """Consume actual current sources while the sole writer/session still live."""
    root, directory = Path(artifact_root).resolve(), Path(output_directory).resolve()
    _require(
        directory.is_relative_to(root) and not directory.exists(),
        "fresh checkpoint output directory required",
    )
    live_capture._open()
    _require(
        live_capture._clock == live_clock.monotonic
        and live_capture.clock_id == clock_id
        and live_clock.session_id == clock_id
        and threading.get_ident() == live_capture._owner,
        "actual same main writer/live clock required",
    )
    now = require_real(now_s, "checkpoint qualification time", minimum=0)
    _require(
        0 <= live_clock.monotonic() - now <= 0.05, "qualification time is not current live clock"
    )
    prefix = strict_json_loads(_bound_bytes(root, prefix_source, 32 * 1024 * 1024, {}))
    _prefix_writer_identity(prefix, live_capture)
    configured_wind = _configured_wind(root, world_source, runtime_source)
    profile = strict_json_loads(_bound_bytes(root, profile_source, 65536, {}))
    directory.mkdir()
    resource = {
        "clock_id": clock_id,
        "transaction_id": prefix["transaction_id"],
        "observed_at_s": now,
        "airframe_profile_hash": airframe_profile_hash(profile),
        "camera_available": True,
        "geofence_valid": False,
        "perception_available": False,
        "map_cache_available": False,
        "dem_available": False,
        "wind_north_mps": configured_wind.wind_north_mps,
        "wind_east_mps": configured_wind.wind_east_mps,
        "wind_source": "software-SIM-configured",
        "configured_wind": configured_wind.to_mapping(),
        "battery_packet_sha256": prefix["snapshot_rows"]["BATTERY_STATUS"]["packet_sha256"],
    }
    resource_source = _save_json(root, directory / "limited-resources.json", resource)
    inputs = {
        "prefix_source": prefix_source.to_mapping(),
        "profile_source": profile_source.to_mapping(),
        "runtime_source": runtime_source.to_mapping(),
        "world_source": world_source.to_mapping(),
        "model_sources": {k: v.to_mapping() for k, v in model_sources.items()},
        "camera_updates": camera_updates,
        "parameter_baseline": parameter_baseline,
        "navigation_before": navigation_before,
        "navigation_after": navigation_after,
        "owned_inspect_before": owned_inspect_before,
        "owned_inspect_after": owned_inspect_after,
        "plan": plan,
        "container_id": container_id,
        "owner_token": owner_token,
        "resource_source": resource_source.to_mapping(),
    }
    input_source = _save_json(root, directory / "checkpoint-inputs.json", inputs)
    result = _recompute(root, inputs, input_source, now, clock_id)
    origin_source = _save_json(root, directory / "navigation-origin.json", result["origin"])
    document = {
        "contract": CONTRACT,
        "clock_id": clock_id,
        "qualified_at_s": now,
        "inputs": inputs,
        "input_source": input_source.to_mapping(),
        "origin_source": origin_source.to_mapping(),
        **result,
        "claims": {
            "execution_authority": False,
            "Grant_created": False,
            "Mission_approved": False,
            "ARM_sent": False,
            "flight_executed": False,
            "map_dem_geofence_qualified": False,
            "physical_performance_claimed": False,
            "final_release_allowed": False,
        },
    }
    source = _save_json(root, directory / "live-navigation-checkpoint.json", document)
    checkpoint = LiveTeacherNavigationCheckpoint(
        canonical_json(document), source, live_capture, live_clock
    )
    validate_live_teacher_navigation_checkpoint(
        checkpoint, artifact_root=root, now_s=live_clock.monotonic(), clock_id=clock_id
    )
    return checkpoint


def validate_live_teacher_navigation_checkpoint(
    checkpoint, *, artifact_root, now_s, clock_id, expected_origin_continuity_token=None
):
    """Recompute exact original semantics without renewing time or identity.

    An exact historical qualified_at_s recomputation is permitted for original
    draft identity comparison only. A current check requires its still-open
    actual writer/clock handle and never turns a loaded receipt into live data.
    """
    _require(
        type(checkpoint) is LiveTeacherNavigationCheckpoint, "typed actual checkpoint required"
    )
    root = Path(artifact_root).resolve()
    now = require_real(now_s, "live revalidation time", minimum=0)
    document = strict_json_loads(
        _bound_bytes(root, checkpoint.checkpoint_source, 2 * 1024 * 1024, {})
    )
    _require(
        document == checkpoint.to_mapping()
        and document["contract"] == CONTRACT
        and document["clock_id"] == clock_id,
        "checkpoint original source/clock differs",
    )
    original_now = document["qualified_at_s"]
    _require(original_now <= now < document["expires_at_s"], "original checkpoint expired/future")
    if now != original_now:
        _require(
            checkpoint._live_capture is not None and checkpoint._live_clock is not None,
            "historical receipt cannot become a live checkpoint",
        )
        checkpoint._live_capture._open()
        _require(
            checkpoint._live_capture._clock == checkpoint._live_clock.monotonic
            and checkpoint._live_capture.clock_id == clock_id
            and 0 <= checkpoint._live_clock.monotonic() - now <= 0.05,
            "current checkpoint writer/clock changed",
        )
    input_source = EvidenceBinding.from_mapping(document["input_source"])
    inputs = strict_json_loads(_bound_bytes(root, input_source, 128 * 1024, {}))
    _require(inputs == document["inputs"], "current checkpoint inputs differ")
    if now != original_now:
        prefix_source = EvidenceBinding.from_mapping(inputs["prefix_source"])
        prefix = strict_json_loads(_bound_bytes(root, prefix_source, 32 * 1024 * 1024, {}))
        _prefix_writer_identity(prefix, checkpoint._live_capture)
    recomputed = _recompute(root, inputs, input_source, original_now, clock_id)
    _require(
        all(document[k] == v for k, v in recomputed.items()),
        "checkpoint semantic/hash derivation differs",
    )
    saved_origin = strict_json_loads(_bound_bytes(root, checkpoint.origin_source, 128 * 1024, {}))
    _require(saved_origin == recomputed["origin"], "origin original source differs")
    if expected_origin_continuity_token is not None:
        _require(
            checkpoint.origin_continuity_token == expected_origin_continuity_token,
            "original plan HOME/reference/reset token changed",
        )
    if now != original_now:
        final_now = checkpoint._live_clock.monotonic()
        checkpoint._live_capture._open()
        _require(
            now <= final_now < checkpoint.expires_at_s,
            "live evidence expired while originals were revalidated",
        )
    return checkpoint


__all__ = [
    "LiveTeacherNavigationCheckpoint",
    "capture_teacher_committed_prefix",
    "qualify_teacher_live_navigation_checkpoint",
    "validate_live_teacher_navigation_checkpoint",
    "navigation_parameter_baseline_argv",
    "navigation_reference_phase_argv",
    "decode_context_packet",
]
