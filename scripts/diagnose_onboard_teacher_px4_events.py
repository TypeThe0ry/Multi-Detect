"""Bounded, offline-only PX4 EVENT diagnostics; never re-admit original UNKNOWNs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import struct
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.teacher-px4-offline-event-diagnostics-v1"
CAPTURE_CONTRACT = "multidetect.onboard-teacher-raw-telemetry-v3"
IMAGE = (
    "px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8"
)
REVISION = "28eae47457249ab6c8c1cadd104fcbe25eacd7a3"
HEADER_DIR = ROOT / "artifacts/diagnostics/onboard-teacher-px4-event-definitions-20261008-r1"
HEADERS = {
    410: (
        "mavlink_msg_event.h",
        19560,
        "0e9b9405e0bd7da44f6dfead813e288dcb45ccbbae6cc8aa4b13a78e073380dc",
        53,
        160,
    ),
    411: (
        "mavlink_msg_current_event_sequence.h",
        12475,
        "233be842c93e993a90ffef15e34263a95e0e892971a38afbebfebb8c67853f52",
        3,
        106,
    ),
}
CAPTURE = (
    ROOT
    / "artifacts/diagnostics/onboard-teacher-gazebo-observation-20261008-r3"
    / "telemetry/capture-receipt.json"
)
CAPTURE_SHA = "bc1e4b2351803c75a3e505f0f6ad7d8838744ed75c43f62ac000ac03478bb2ca"
METADATA_DIR = ROOT / "artifacts/diagnostics/onboard-teacher-px4-image-metadata-20261008-r2"
METADATA_SHA = "4255908f58f4a05a799ccf4ca304e10e44304af75a6a665f6f87d22fe610854e"
EXTRACTION_SHA = "30e73f44759de35a6f6953233779d70834f90db5b7400589d1969dad6f127927"
TYPES = {
    "uint8_t": "B",
    "int8_t": "b",
    "uint16_t": "H",
    "int16_t": "h",
    "uint32_t": "I",
    "int32_t": "i",
    "uint64_t": "Q",
    "int64_t": "q",
    "float": "f",
}
CLAIMS = {
    key: False
    for key in (
        "original_classification_changed",
        "source_re_admitted",
        "source_authenticated",
        "context_created",
        "flight_authority",
        "arming_permission",
        "actual_live_firmware_verified",
        "firmware_build_reproduced",
        "actual_parameter_values_observed",
        "root_cause_proven",
        "optimizer_performed",
        "model_loaded",
        "final_release_allowed",
    )
}


class DiagnosticError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise DiagnosticError(message)


def integer(value, low, high):
    require(type(value) is int and low <= value <= high, "invalid integer")
    return value


def canonical(value):
    return (
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        + "\n"
    ).encode("utf-8")


def strict_json(raw, *, cap=16 * 1024 * 1024):
    require(len(raw) <= cap, "JSON byte cap")

    def pairs(items):
        out = {}
        for key, value in items:
            require(key not in out, "duplicate JSON key")
            out[key] = value
        return out

    def constant(_):
        raise DiagnosticError("nonfinite JSON constant")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, RecursionError, json.JSONDecodeError) as error:
        raise DiagnosticError("invalid bounded JSON") from error
    stack, nodes = [(value, 0)], 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        require(nodes <= 2_000_000 and depth <= 32, "JSON structure cap")
        if type(current) is float:
            require(math.isfinite(current), "nonfinite JSON number")
        elif type(current) is dict:
            stack.extend((v, depth + 1) for v in current.values())
        elif type(current) is list:
            stack.extend((v, depth + 1) for v in current)
    return value


def _identity(stat):
    return stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino


def safe_path(path):
    path = Path(path).resolve()
    require(path.is_relative_to(ROOT.resolve()), "source outside repository")
    return path


def read_bound(path, *, cap, sha256=None, size=None, inventory=None):
    path = Path(path).resolve()
    before = path.stat()
    require(before.st_size <= cap, "source byte cap")
    with path.open("rb") as stream:
        raw = stream.read(cap + 1)
    require(
        len(raw) <= cap and _identity(before) == _identity(path.stat()), "source changed on read"
    )
    digest = hashlib.sha256(raw).hexdigest()
    require(sha256 is None or digest == sha256, "source SHA mismatch")
    require(size is None or type(size) is int and len(raw) == size, "source size mismatch")
    record = {"path": path.as_posix(), "bytes": len(raw), "sha256": digest}
    if inventory is not None:
        old = inventory.get(path.as_posix())
        require(old is None or old == record, "conflicting source binding")
        inventory[path.as_posix()] = record
    return raw, record


def crc16(raw):
    """CRC-16/MCRF4XX, same recurrence as pinned pymavlink x25crc_slow."""
    crc = 0xFFFF
    for byte in raw:
        tmp = byte ^ (crc & 0xFF)
        tmp = (tmp ^ (tmp << 4)) & 0xFF
        crc = (crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)
    return crc


def validate_mavlink2_frame(packet, *, expected_message_id, crc_extra, max_payload_length):
    """Validate unsigned wire bytes; payload_raw is original, not zero-extended."""
    integer(expected_message_id, 0, 0xFFFFFF)
    integer(crc_extra, 0, 255)
    integer(max_payload_length, 1, 255)
    require(type(packet) is bytes and 13 <= len(packet) <= 280, "packet length cap")
    require(packet[0] == 0xFD, "EVENT requires MAVLink2")
    # No key/authentication claim is possible here; signed packets are explicitly refused.
    require(packet[2] == 0, "signed or unsupported incompatibility flags")
    length = packet[1]
    require(length >= 1 and len(packet) == length + 12, "wire length/trailing bytes")
    message_id = int.from_bytes(packet[7:10], "little")
    require(message_id == expected_message_id, "unexpected message ID")
    require(length <= max_payload_length, "payload exceeds pinned definition")
    expected = int.from_bytes(packet[-2:], "little")
    actual = crc16(packet[1:-2] + bytes([crc_extra]))
    require(expected == actual, "packet CRC mismatch")
    return {
        "message_id": message_id,
        "wire_system_id": packet[5],
        "wire_component_id": packet[6],
        "wire_packet_sequence": packet[4],
        "wire_payload_bytes": length,
        "payload_raw": packet[10:-2],
        "crc_extra": crc_extra,
        "crc16": actual,
        "crc_verified": True,
        "authentication_verified": False,
        "compat_flags": packet[3],
    }


def decode_packet(packet):
    require(type(packet) is bytes and len(packet) >= 10, "packet header truncated")
    message_id = int.from_bytes(packet[7:10], "little")
    require(message_id in HEADERS, "not an EVENT/current-sequence packet")
    full_length, extra = HEADERS[message_id][3:]
    result = validate_mavlink2_frame(
        packet, expected_message_id=message_id, crc_extra=extra, max_payload_length=full_length
    )
    payload = result.pop("payload_raw").ljust(full_length, b"\x00")
    result["zero_extended_bytes"] = full_length - result["wire_payload_bytes"]
    if message_id == 411:
        sequence, flags = struct.unpack("<HB", payload)
        result.update(
            sequence=sequence, flags=flags, reset=bool(flags & 1), unknown_flag_bits=flags & ~1
        )
    else:
        event_id, boot_ms, sequence, component, system, levels = struct.unpack(
            "<IIHBBB", payload[:13]
        )
        result.update(
            event_id=event_id,
            event_sub_id=event_id & 0xFFFFFF,
            namespace_component_id=event_id >> 24,
            event_time_boot_ms=boot_ms,
            sequence=sequence,
            destination_component=component,
            destination_system=system,
            log_level_internal=levels >> 4,
            log_level_external=levels & 15,
            arguments_hex=payload[13:].hex(),
        )
    return result


def _numeric_key(key, maximum):
    require(type(key) is str and re.fullmatch(r"0|[1-9][0-9]*", key), "noncanonical numeric key")
    return integer(int(key), 0, maximum)


def _text(value, cap=8192):
    require(type(value) is str and len(value.encode("utf-8")) <= cap, "invalid metadata text")
    return value


def _time(value):
    require(
        type(value) in (int, float) and math.isfinite(value) and value >= 0,
        "invalid received timestamp",
    )
    return value


def validate_metadata(document):
    require(
        type(document) is dict and set(document) <= {"version", "components", "translation"},
        "metadata top-level schema",
    )
    require(
        type(document.get("version")) is int and document["version"] == 2,
        "unsupported metadata version",
    )
    components = document.get("components")
    require(type(components) is dict and 1 <= len(components) <= 256, "metadata components")
    events, enums, namespaces = {}, {}, set()
    for cid, component in components.items():
        component_id = _numeric_key(cid, 255)
        require(
            type(component) is dict
            and set(component)
            <= {
                "namespace",
                "enums",
                "event_groups",
                "navigation_mode_groups",
                "supported_protocols",
            },
            "component schema",
        )
        namespace = _text(component.get("namespace"), 128)
        require(namespace and namespace not in namespaces, "duplicate/empty namespace")
        namespaces.add(namespace)
        require(type(component.get("enums", {})) is dict, "enum map")
        for name, definition in component.get("enums", {}).items():
            _text(name, 128)
            require(
                type(definition) is dict
                and set(definition)
                <= {"type", "description", "is_bitfield", "separator", "entries"},
                "enum schema",
            )
            base = definition.get("type")
            require(base in TYPES and base != "float", "enum base type")
            require(type(definition.get("is_bitfield", False)) is bool, "enum bitfield type")
            require(type(definition.get("entries", {})) is dict, "enum entries")
            bits = struct.calcsize(TYPES[base]) * 8
            low, high = (
                (0, (1 << bits) - 1)
                if base.startswith("u")
                else (-(1 << (bits - 1)), (1 << (bits - 1)) - 1)
            )
            for key, entry in definition.get("entries", {}).items():
                require(
                    type(key) is str and re.fullmatch(r"0|-?[1-9][0-9]*", key), "enum value key"
                )
                integer(int(key), low, high)
                require(
                    type(entry) is dict and set(entry) <= {"name", "description", "comment"},
                    "enum entry schema",
                )
                _text(entry.get("name"), 128)
                _text(entry.get("description", ""))
            enums[namespace + "::" + name] = definition
        groups = component.get("event_groups", {})
        require(type(groups) is dict, "event groups")
        for group, value in groups.items():
            _text(group, 128)
            require(type(value) is dict and set(value) <= {"events"}, "event group schema")
            require(type(value.get("events", {})) is dict, "events map")
            for sub, event in value.get("events", {}).items():
                eid = (component_id << 24) | _numeric_key(sub, 0xFFFFFF)
                require(eid not in events, "duplicate event ID across groups")
                require(
                    type(event) is dict
                    and set(event)
                    <= {
                        "name",
                        "message",
                        "description",
                        "arguments",
                        "type",
                        "instance_arg_index",
                    },
                    "event schema",
                )
                _text(event.get("name"), 128)
                _text(event.get("message"), 1024)
                _text(event.get("description", ""))
                require(
                    type(event.get("arguments", [])) is list
                    and len(event.get("arguments", [])) <= 40,
                    "argument count",
                )
                for arg in event.get("arguments", []):
                    require(
                        type(arg) is dict and set(arg) <= {"name", "type", "description"},
                        "argument schema",
                    )
                    _text(arg.get("name"), 128)
                    _text(arg.get("type"), 128)
                events[eid] = {"namespace": namespace, "group": group, "definition": event}
                require(len(events) <= 20_000, "event count cap")
    for info in events.values():
        size = 0
        for arg in info["definition"].get("arguments", []):
            base, _ = argument_type(arg["type"], info["namespace"], enums)
            size += struct.calcsize("<" + TYPES[base])
        require(size <= 40, "argument byte cap")
    return events, enums


def argument_type(name, namespace, enums):
    if name in TYPES:
        return name, None
    key = name if "::" in name else namespace + "::" + name
    require(key in enums, "unknown argument type/enum")
    return enums[key]["type"], enums[key]


def decode_arguments(event_id, raw, events, enums):
    require(type(raw) is bytes and len(raw) == 40, "argument buffer length")
    if event_id not in events:
        return {"metadata_matched": False, "arguments_hex": raw.hex()}
    info = events[event_id]
    event, values, offset = info["definition"], [], 0
    for arg in event.get("arguments", []):
        base, enum = argument_type(arg["type"], info["namespace"], enums)
        size = struct.calcsize("<" + TYPES[base])
        chunk = raw[offset : offset + size]
        value = struct.unpack("<" + TYPES[base], chunk)[0]
        row = {
            "name": arg["name"],
            "declared_type": arg["type"],
            "base_type": base,
            "offset": offset,
            "bytes_hex": chunk.hex(),
            "value": value,
        }
        if base == "float":
            row.update(ieee754_f32_bits=chunk.hex(), finite=math.isfinite(value))
            if not math.isfinite(value):
                row["value"] = (
                    None  # raw IEEE bits are authoritative; never substitute a finite value.
                )
        if enum:
            entries = enum.get("entries", {})
            if enum.get("is_bitfield", False):
                known_mask, labels = 0, []
                for key, entry in entries.items():
                    bit = int(key)
                    if bit > 0 and bit & (bit - 1) == 0:
                        known_mask |= bit
                        if value & bit:
                            labels.append(entry["name"])
                row.update(enum_labels=labels, unknown_bits=value & ~known_mask)
            else:
                entry = entries.get(str(value))
                row.update(
                    enum_name=entry["name"] if entry else None, enum_value_known=entry is not None
                )
        values.append(row)
        offset += size
    return {
        "metadata_matched": True,
        "namespace": info["namespace"],
        "group": info["group"],
        "name": event["name"],
        "message_template": event["message"],
        "description_template": event.get("description", ""),
        "arguments": values,
        "argument_bytes_used": offset,
        "unused_argument_bytes_hex": raw[offset:].hex(),
        "text_rendered": False,
    }


def sequence_observation(decoded, states):
    key = f"{decoded['wire_system_id']}:{decoded['wire_component_id']}"
    previous = states.get(key)
    sequence = decoded["sequence"]
    if decoded.get("reset", False) or previous is None:
        states[key] = sequence
        return {
            "sender": key,
            "status": "reset" if previous is not None else "initial",
            "previous": previous,
            "current": sequence,
            "missing_count": 0,
        }
    delta = (sequence - previous) & 0xFFFF
    status = (
        "duplicate_or_current"
        if delta == 0
        else (
            "next"
            if delta == 1
            else "forward_gap"
            if delta < 32768
            else "ambiguous_half_range"
            if delta == 32768
            else "older_or_retransmitted"
        )
    )
    if decoded["message_id"] == 410 and 0 < delta < 32768:
        states[key] = sequence
    return {
        "sender": key,
        "status": status,
        "previous": previous,
        "current": sequence,
        "wrapped": sequence < previous and 0 < delta < 32768,
        "missing_count": max(0, delta - 1)
        if decoded["message_id"] == 410 and delta < 32768
        else None,
    }


def _slice(raw, binding):
    offset = integer(binding.get("offset"), 0, len(raw))
    size = integer(binding.get("bytes"), 1, 65536)
    require(offset + size <= len(raw), "slice exceeds source")
    chunk = raw[offset : offset + size]
    require(hashlib.sha256(chunk).hexdigest() == binding.get("sha256"), "slice SHA mismatch")
    return chunk


def diagnose(
    capture_path,
    capture_sha256,
    metadata_path,
    metadata_sha256,
    extraction_path,
    extraction_sha256,
    *,
    header_directory=HEADER_DIR,
    max_packets=24,
):
    integer(max_packets, 1, 24)
    inventory = {}
    capture_raw, _ = read_bound(
        safe_path(capture_path), cap=16 * 1024 * 1024, sha256=capture_sha256, inventory=inventory
    )
    capture = strict_json(capture_raw)
    require(
        capture.get("contract") == CAPTURE_CONTRACT
        and capture.get("status") == "recorded"
        and capture.get("error") is None
        and capture.get("evidence_error") is None,
        "capture not normally sealed",
    )
    records = capture.get("records")
    require(
        type(records) is list
        and len(records) <= 8192
        and type(capture.get("record_count")) is int
        and capture["record_count"] == len(records),
        "capture record coverage",
    )
    require(
        all(
            type(row.get("record_index")) is int and row["record_index"] == i
            for i, row in enumerate(records)
        ),
        "capture ordered indices",
    )
    extraction_raw, _ = read_bound(
        safe_path(extraction_path), cap=1024 * 1024, sha256=extraction_sha256, inventory=inventory
    )
    extraction = strict_json(extraction_raw)
    require(
        extraction.get("contract") == "multidetect.teacher-pinned-PX4-metadata-extraction-v1"
        and extraction.get("status") == "pinned-image-metadata-extracted"
        and extraction.get("image") == IMAGE
        and extraction.get("container_started") is False
        and extraction.get("cleanup_verified") is True
        and extraction.get("error") is None,
        "metadata extraction provenance",
    )
    declared = extraction["files"]["all_events.json.xz"]["expanded"]
    require(
        safe_path(declared["path"]) == safe_path(metadata_path)
        and declared["sha256"] == metadata_sha256,
        "metadata/extraction binding",
    )
    metadata_raw, _ = read_bound(
        safe_path(metadata_path),
        cap=2 * 1024 * 1024,
        sha256=metadata_sha256,
        size=declared["bytes"],
        inventory=inventory,
    )
    events, enums = validate_metadata(strict_json(metadata_raw, cap=2 * 1024 * 1024))
    for message_id, (name, size, digest, length, extra) in HEADERS.items():
        header, _ = read_bound(
            safe_path(Path(header_directory) / name),
            cap=65536,
            sha256=digest,
            size=size,
            inventory=inventory,
        )
        require(
            f"#define MAVLINK_MSG_ID_{message_id}_LEN {length}".encode() in header
            and f"#define MAVLINK_MSG_ID_{message_id}_CRC {extra}".encode() in header,
            "pinned header definition mismatch",
        )
    journals = {}
    for role in ("raw", "projection"):
        source = capture["journal_sources"][role]
        require(
            source.get("verified_prefix_matches") is True
            and source.get("last_verified_file_identity_matches") is True
            and type(source.get("uncommitted_tail_bytes")) is int
            and source["uncommitted_tail_bytes"] == 0,
            "journal not closed",
        )
        journals[role], _ = read_bound(
            safe_path(source["path"]),
            cap=64 * 1024 * 1024,
            sha256=source["sha256"],
            size=source["bytes"],
            inventory=inventory,
        )
    require(journals["raw"].startswith(b"MDTEACHER-RAW-V3\n"), "raw journal magic")
    selected = [row for row in records if row.get("message_type") in ("UNKNOWN_410", "UNKNOWN_411")]
    require(1 <= len(selected) <= max_packets, "selected packet count cap")
    rows, states = [], {}
    for row in selected:
        require(
            row.get("source_accepted") is False and row.get("status") == "dual-committed",
            "original UNKNOWN admission changed",
        )
        raw_binding, projection_binding = row["raw_record"], row["projection_record"]
        require(
            safe_path(raw_binding["path"]) == safe_path(capture["journal_sources"]["raw"]["path"])
            and safe_path(projection_binding["path"])
            == safe_path(capture["journal_sources"]["projection"]["path"]),
            "slice source path mismatch",
        )
        frame = _slice(journals["raw"], raw_binding)
        header_size = struct.unpack("<I", frame[:4])[0]
        require(
            1 <= header_size <= 4096 and 4 + header_size < len(frame), "raw frame header length"
        )
        header = strict_json(frame[4 : 4 + header_size], cap=4096)
        packet = frame[4 + header_size :]
        _time(row.get("received_at_s"))
        _time(header.get("received_at_s"))
        require(
            header.get("contract") == CAPTURE_CONTRACT
            and header.get("record_index") == row["record_index"]
            and type(header.get("record_index")) is int
            and header.get("received_at_s") == row["received_at_s"]
            and raw_binding.get("packet_offset") == raw_binding["offset"] + 4 + header_size
            and type(raw_binding.get("packet_offset")) is int,
            "raw frame identity mismatch",
        )
        digest = hashlib.sha256(packet).hexdigest()
        require(
            all(
                value == digest
                for value in (
                    row["packet_sha256"],
                    raw_binding["packet_sha256"],
                    header["packet_sha256"],
                )
            )
            and all(
                type(value) is int and value == len(packet)
                for value in (
                    row["packet_bytes"],
                    raw_binding["packet_bytes"],
                    header["packet_bytes"],
                )
            ),
            "packet binding mismatch",
        )
        projection = strict_json(_slice(journals["projection"], projection_binding), cap=65536)
        envelope = projection["row"]
        _time(envelope.get("received_at_s"))
        require(
            projection.get("raw_record") == raw_binding
            and envelope.get("source_accepted") is False
            and envelope.get("message", {}).get("message_type") == row["message_type"]
            and envelope.get("packet_sha256") == digest
            and type(envelope.get("record_index")) is int
            and envelope["record_index"] == row["record_index"]
            and envelope.get("received_at_s") == row["received_at_s"],
            "projection identity mismatch",
        )
        decoded = decode_packet(packet)
        require(row["message_type"] == f"UNKNOWN_{decoded['message_id']}", "message ID mismatch")
        result = {
            "record_index": row["record_index"],
            "original_kind": row["message_type"],
            "original_source_accepted": False,
            "original_decoder_system_id": envelope["system_id"],
            "original_decoder_component_id": envelope["component_id"],
            "received_at_s": row["received_at_s"],
            "packet_sha256": digest,
            "raw_record": raw_binding,
            "projection_record": projection_binding,
            "derived": decoded,
            "sequence_observation": sequence_observation(decoded, states),
        }
        if decoded["message_id"] == 410:
            result["metadata"] = decode_arguments(
                decoded["event_id"], bytes.fromhex(decoded["arguments_hex"]), events, enums
            )
        rows.append(result)
    for path in (Path(__file__), ROOT / "tests/test_diagnose_onboard_teacher_px4_events.py"):
        read_bound(path, cap=1024 * 1024, inventory=inventory)
    after = {}
    for source in inventory.values():
        read_bound(
            source["path"],
            cap=64 * 1024 * 1024,
            sha256=source["sha256"],
            size=source["bytes"],
            inventory=after,
        )
    require(inventory == after, "final sources changed")
    named = Counter(
        row.get("metadata", {}).get("name")
        for row in rows
        if row.get("metadata", {}).get("metadata_matched")
    )
    groups = Counter(
        row.get("metadata", {}).get("group")
        for row in rows
        if row.get("metadata", {}).get("metadata_matched")
    )
    return {
        "contract": CONTRACT,
        "success": True,
        "claims": CLAIMS.copy(),
        "scope": (
            "Offline derived diagnostics, not live firmware, authentication, "
            "parameter values, or permission"
        ),
        "definition_revision": REVISION,
        "metadata_image": IMAGE,
        "source_check_scope": (
            "Full bounded file bytes/SHA before and after decoding, before publication"
        ),
        "sources_before": inventory,
        "sources_after": after,
        "sources_unchanged": True,
        "record_count": len(rows),
        "message_counts": dict(Counter(row["original_kind"] for row in rows)),
        "metadata_event_counts": dict(named),
        "metadata_group_counts": dict(groups),
        "manual_control_input_event_count": named["check_modes_manual_control"],
        "all_selected_crc_verified": all(row["derived"]["crc_verified"] for row in rows),
        "rows": rows,
    }


def write_new(path, report):
    path = safe_path(path)
    require(
        path.is_relative_to((ROOT / "artifacts/diagnostics").resolve()) and path.suffix == ".json",
        "output must be a new diagnostics JSON",
    )
    require(not path.exists(), "output already exists")
    data = canonical(report)
    require(len(data) <= 2 * 1024 * 1024, "output byte cap")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        require(stream.write(data) == len(data), "short output write")
        stream.flush()
        os.fsync(stream.fileno())
    _, binding = read_bound(
        path, cap=2 * 1024 * 1024, sha256=hashlib.sha256(data).hexdigest(), size=len(data)
    )
    return binding


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-receipt", type=Path, default=CAPTURE)
    parser.add_argument("--capture-sha256", default=CAPTURE_SHA)
    parser.add_argument("--metadata", type=Path, default=METADATA_DIR / "all_events.json")
    parser.add_argument("--metadata-sha256", default=METADATA_SHA)
    parser.add_argument(
        "--extraction-receipt", type=Path, default=METADATA_DIR / "metadata-extraction.json"
    )
    parser.add_argument("--extraction-sha256", default=EXTRACTION_SHA)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        require(not safe_path(args.output).exists(), "output already exists")
        report = diagnose(
            args.capture_receipt,
            args.capture_sha256,
            args.metadata,
            args.metadata_sha256,
            args.extraction_receipt,
            args.extraction_sha256,
        )
        print(json.dumps(write_new(args.output, report), sort_keys=True))
        return 0
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
