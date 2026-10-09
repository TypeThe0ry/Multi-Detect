"""Offline parameter interpretation from CLOSED dual-committed original packets.

Capabilities, integer union bits and source identities are checked separately.
Absent/unproven values remain unknown. No receive loop, write request, model,
flight permission, context qualification or physical calibration is performed.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from scripts.diagnose_onboard_teacher_px4_events import (  # noqa: E402
    canonical,
    read_bound,
    require,
    strict_json,
    validate_mavlink2_frame,
)
from scripts.run_onboard_teacher_gazebo_observation_v2 import FALSE_CLAIMS, _json_new  # noqa: E402
from scripts.run_onboard_teacher_px4_diagnostics import PARAMETERS  # noqa: E402

CONTRACT = "multidetect.teacher-PX4-offline-parameter-diagnostics-v1"
COMMON = (
    ROOT / ".venv-gazebo-navigation-20261008-r1/Lib/site-packages/pymavlink/dialects/v20/common.py"
)
COMMON_SHA = "728a16d65ff4e531a01cc6f7111d909179eec1b0498b4c4a49a13d1388f798c3"
COMMON_BYTES = 1800728


def decode_response_packet(packet, *, parameter_encoding=None):
    require(type(packet) is bytes and len(packet) >= 10, "bounded MAVLink2 response required")
    message_id = int.from_bytes(packet[7:10], "little")
    require(message_id in {22, 148}, "only PARAM_VALUE/AUTOPILOT_VERSION responses")
    length, extra = (25, 220) if message_id == 22 else (78, 178)
    wire = validate_mavlink2_frame(
        packet,
        expected_message_id=message_id,
        crc_extra=extra,
        max_payload_length=length,
    )
    payload = wire.pop("payload_raw").ljust(length, b"\0")
    wire["zero_extended_bytes"] = length - wire["wire_payload_bytes"]
    if message_id == 148:
        caps, uid, flight, middleware, os_version, board, vendor, product = struct.unpack(
            "<QQIIIIHH",
            payload[:36],
        )
        bytewise, c_cast = bool(caps & 16), bool(caps & 131072)
        wire.update(
            capabilities=caps,
            uid=uid,
            flight_sw_version=flight,
            middleware_sw_version=middleware,
            os_sw_version=os_version,
            board_version=board,
            vendor_id=vendor,
            product_id=product,
            flight_custom_version_hex=payload[36:44].hex(),
            middleware_custom_version_hex=payload[44:52].hex(),
            os_custom_version_hex=payload[52:60].hex(),
            uid2_hex=payload[60:78].hex(),
            parameter_encoding=("bytewise" if bytewise else "c_cast")
            if bytewise != c_cast
            else None,
            encoding_claim_ambiguous=bytewise and c_cast,
        )
        return wire
    count, index = struct.unpack("<HH", payload[4:8])
    raw_name = payload[8:24]
    prefix, separator, tail = raw_name.partition(b"\0")
    require(not separator or not tail.strip(b"\0"), "nonzero parameter ID padding")
    name = prefix.decode("ascii", errors="strict")
    require(re.fullmatch(r"[A-Z][A-Z0-9_]{0,15}", name) is not None, "parameter ID invalid")
    require(count > 0 and index < count, "parameter index/count invalid")
    kind = payload[24]
    raw = payload[:4]
    wire.update(
        name=name,
        param_type=kind,
        reported_count=count,
        index=index,
        raw_union_hex=raw.hex(),
        value=None,
        interpretation_status="encoding-unproven",
        parameter_encoding=parameter_encoding,
    )
    require(parameter_encoding in {None, "bytewise", "c_cast"}, "unknown parameter encoding")
    if parameter_encoding is None:
        return wire
    if kind not in {6, 9}:
        wire["interpretation_status"] = "unsupported-parameter-type"
        return wire
    if kind == 6 and parameter_encoding == "bytewise":
        value = struct.unpack("<i", raw)[0]
    else:
        value = struct.unpack("<f", raw)[0]
        if not math.isfinite(value):
            wire["interpretation_status"] = "nonfinite-value-retained-as-raw-bits"
            return wire
        if kind == 6:
            if value != int(value) or not -(1 << 31) <= value < (1 << 31):
                wire["interpretation_status"] = "invalid-cast-integer"
                return wire
            value = int(value)
    wire.update(value=value, interpretation_status="decoded-from-original-CRC-verified-packet")
    return wire


def diagnose_parameters(capture_path, capture_sha256, output_path):
    inventory = {}
    capture_path, output_path = Path(capture_path).resolve(), Path(output_path).resolve()
    require(
        capture_path.is_relative_to(ROOT) and output_path.is_relative_to(ROOT),
        "contained paths required",
    )
    require(not output_path.exists(), "new derived evidence required")
    original, binding = read_bound(
        capture_path,
        cap=16 * 1024 * 1024,
        sha256=capture_sha256,
        inventory=inventory,
    )
    receipt = strict_json(original)
    require(
        receipt["contract"] == "multidetect.onboard-teacher-raw-telemetry-v3",
        "capture contract differs",
    )
    require(
        receipt["status"] == "recorded" and receipt["orphan_packet_count"] == 0,
        "closed complete capture required",
    )
    _, definition = read_bound(
        COMMON,
        cap=2 * 1024 * 1024,
        size=COMMON_BYTES,
        sha256=COMMON_SHA,
        inventory=inventory,
    )
    for path in (
        Path(__file__).resolve(),
        Path(__file__).with_name("diagnose_onboard_teacher_px4_events.py").resolve(),
    ):
        read_bound(path, cap=2 * 1024 * 1024, inventory=inventory)
    journals = {}
    for stage in ("raw", "projection"):
        source = receipt["journal_sources"][stage]
        require(
            source["uncommitted_tail_bytes"] == 0 and source["verified_prefix_matches"] is True,
            "incomplete journal",
        )
        path = Path(source["path"]).resolve()
        require(path.parent == capture_path.parent, "journal ownership directory differs")
        journals[stage], _ = read_bound(
            path,
            cap=64 * 1024 * 1024,
            size=source["bytes"],
            sha256=source["sha256"],
            inventory=inventory,
        )
    decoded, errors = [], []
    records = receipt["records"]
    require(
        type(records) is list
        and len(records) <= 8192
        and type(receipt.get("record_count")) is int
        and receipt["record_count"] == len(records)
        and all(
            type(row.get("record_index")) is int and row["record_index"] == index
            for index, row in enumerate(records)
        ),
        "bounded ordered capture record coverage required",
    )
    require(journals["raw"].startswith(b"MDTEACHER-RAW-V3\n"), "raw journal magic differs")
    projection_magic = b'{"contract":"multidetect.teacher-projection-journal-v3"}\n'
    require(journals["projection"].startswith(projection_magic), "projection journal magic differs")
    ends = {"raw": len(b"MDTEACHER-RAW-V3\n"), "projection": len(projection_magic)}
    for record in records:
        require(record["status"] == "dual-committed", "closed capture contains uncommitted record")
        for stage, key in (("raw", "raw_record"), ("projection", "projection_record")):
            slice_binding = record[key]
            require(
                type(slice_binding["offset"]) is int
                and slice_binding["offset"] == ends[stage]
                and type(slice_binding["bytes"]) is int
                and slice_binding["bytes"] > 0,
                "complete ordered journal coverage differs",
            )
            ends[stage] += slice_binding["bytes"]
            require(ends[stage] <= len(journals[stage]), "record extends beyond closed journal")
    require(
        all(ends[stage] == len(journals[stage]) for stage in ends), "unlisted closed journal bytes"
    )
    for record in records:
        require(record["status"] == "dual-committed", "selected record not dual committed")
        require(type(record["source_accepted"]) is bool, "original source admission must be bool")
        raw_slice, projected = record["raw_record"], record["projection_record"]
        for stage, slice_binding in (("raw", raw_slice), ("projection", projected)):
            require(
                Path(slice_binding["path"]).resolve()
                == Path(receipt["journal_sources"][stage]["path"]).resolve(),
                "slice journal differs",
            )
            offset, size = slice_binding["offset"], slice_binding["bytes"]
            require(
                type(offset) is int
                and type(size) is int
                and 0 <= offset < len(journals[stage])
                and 0 < size <= len(journals[stage]) - offset,
                "slice bounds invalid",
            )
            require(
                hashlib.sha256(journals[stage][offset : offset + size]).hexdigest()
                == slice_binding["sha256"],
                "selected slice hash differs",
            )
        projection = strict_json(
            journals["projection"][projected["offset"] : projected["offset"] + projected["bytes"]],
            cap=40 * 1024,
        )
        row = projection["row"]
        require(
            row["record_index"] == record["record_index"]
            and type(row["record_index"]) is int
            and type(row["source_accepted"]) is bool
            and row["received_at_s"] == record["received_at_s"]
            and row["clock_id"] == receipt["clock_id"]
            and row["transaction_id"] == receipt["transaction_id"]
            and row["source_accepted"] == record["source_accepted"],
            "original projection identity differs",
        )
        require(
            projection["raw_record"] == raw_slice
            and row["message"]["message_type"] == record["message_type"],
            "projection raw reference or message type differs",
        )
        framed = journals["raw"][raw_slice["offset"] : raw_slice["offset"] + raw_slice["bytes"]]
        require(len(framed) > 4, "raw record header truncated")
        header_size = struct.unpack("<I", framed[:4])[0]
        require(
            0 < header_size <= 4096 and 4 + header_size < len(framed),
            "raw record header length invalid",
        )
        header = strict_json(framed[4 : 4 + header_size], cap=4096)
        for value in (record["received_at_s"], row["received_at_s"], header["received_at_s"]):
            require(
                type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e20,
                "original receive time invalid",
            )
        require(
            header["contract"] == receipt["contract"]
            and type(header["record_index"]) is int
            and header["record_index"] == record["record_index"]
            and header["received_at_s"] == record["received_at_s"],
            "original raw header identity differs",
        )
        start, size = raw_slice["packet_offset"], raw_slice["packet_bytes"]
        require(
            type(start) is int
            and type(size) is int
            and start == raw_slice["offset"] + 4 + header_size
            and start + size == raw_slice["offset"] + raw_slice["bytes"]
            and type(header["packet_bytes"]) is int
            and type(record["packet_bytes"]) is int
            and size == header["packet_bytes"] == record["packet_bytes"],
            "packet slice outside raw record",
        )
        packet = journals["raw"][start : start + size]
        require(
            hashlib.sha256(packet).hexdigest()
            == raw_slice["packet_sha256"]
            == row["packet_sha256"]
            == header["packet_sha256"]
            == record["packet_sha256"],
            "original packet hash differs",
        )
        # Close every record's original label before selecting responses. A
        # conflicting version cannot be hidden by relabelling it HEARTBEAT.
        if record["message_type"] not in {"AUTOPILOT_VERSION", "PARAM_VALUE"}:
            continue
        if row["source_accepted"] is not True:
            errors.append(
                {
                    "record_index": record["record_index"],
                    "error": "original foreign source retained; not re-admitted",
                }
            )
            continue
        try:
            wire = decode_response_packet(packet)
            require(
                (wire["wire_system_id"], wire["wire_component_id"])
                == (row["system_id"], row["component_id"])
                == (1, 1)
                and type(row["system_id"]) is int
                and type(row["component_id"]) is int,
                "wire/projection source differs",
            )
            require(
                wire["wire_packet_sequence"] == row["packet_sequence"]
                and type(row["packet_sequence"]) is int,
                "wire/projection sequence differs",
            )
            expected = 148 if record["message_type"] == "AUTOPILOT_VERSION" else 22
            require(wire["message_id"] == expected, "wire/projection kind differs")
            decoded.append(
                {
                    "record_index": record["record_index"],
                    "received_at_s": record["received_at_s"],
                    "packet": packet,
                    "wire": wire,
                }
            )
        except (ValueError, UnicodeError) as error:
            errors.append(
                {
                    "record_index": record["record_index"],
                    "error": f"{type(error).__name__}: {error}"[:1024],
                }
            )
    versions = [
        {k: v for k, v in row.items() if k != "packet"}
        for row in decoded
        if row["wire"]["message_id"] == 148
    ]
    # Ignore changing packet sequence when checking a firmware identity; the
    # actual capabilities/firmware fields must still agree throughout capture.
    identities = {
        canonical(
            {
                k: v
                for k, v in row["wire"].items()
                if not k.startswith("wire_")
                and k not in {"crc16", "wire_payload_bytes", "zero_extended_bytes"}
            }
        )
        for row in versions
    }
    encoding = versions[0]["wire"]["parameter_encoding"] if len(identities) == 1 else None
    parameters = []
    for row in decoded:
        if row["wire"]["message_id"] != 22:
            continue
        wire = decode_response_packet(row["packet"], parameter_encoding=encoding)
        if wire["name"] not in PARAMETERS:
            continue
        parameters.append(
            {
                "record_index": row["record_index"],
                "received_at_s": row["received_at_s"],
                "wire": wire,
                "response_causality_verified": False,
            }
        )
    observed = {row["wire"]["name"] for row in parameters}
    for source in inventory.values():
        read_bound(
            source["path"],
            cap=max(source["bytes"], 1),
            size=source["bytes"],
            sha256=source["sha256"],
        )
    report = {
        "contract": CONTRACT,
        "status": "offline-parameter-diagnostics-recorded",
        "capture": binding,
        "definition": definition,
        "sources_reverified": list(inventory.values()),
        "version_rows": versions,
        "version_identity_consistent": len(identities) == 1,
        "parameter_encoding": encoding,
        "parameter_rows": parameters,
        "missing_parameter_names": [name for name in PARAMETERS if name not in observed],
        "uninterpreted_parameter_names": sorted(
            {row["wire"]["name"] for row in parameters if row["wire"]["value"] is None}
        ),
        "errors": errors,
        "source_authenticated": False,
        "closed_snapshot_is_live_context": False,
        "actual_parameter_values_observed": any(
            row["wire"]["value"] is not None for row in parameters
        ),
        "claims": dict(FALSE_CLAIMS),
    }
    report["receipt"] = _json_new(output_path, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--capture-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = diagnose_parameters(args.capture, args.capture_sha256, args.output)
    print(
        canonical(
            {
                key: report[key]
                for key in (
                    "status",
                    "parameter_encoding",
                    "missing_parameter_names",
                    "errors",
                    "receipt",
                )
            }
        ).decode()
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
