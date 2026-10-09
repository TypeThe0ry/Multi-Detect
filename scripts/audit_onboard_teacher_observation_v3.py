#!/usr/bin/env python3
"""Pure-file audit of a CLOSED V3 teacher observation, never a qualification.

No socket, Docker, decoder, image conversion, model or flight operation occurs.
Native exits/ownership remain recorded supervisor evidence, not a new process
observation. The pinned codec checks saved typed projections, not MAVLink CRC or
an independent reconstruction of the original decoder's to_dict() result.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import importlib
import json
import math
import os
import re
import struct
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KINDS = (
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
CODEC = {
    "path": "scripts/onboard_teacher_telemetry_capture_v2.py",
    "bytes": 18202,
    "sha256": "d1a5a4c82086bcff8d9bf89c86ba99902097d424b7a4e3594bd89564d24bb634",
}
CODEC_BASE = {
    "path": "scripts/onboard_teacher_telemetry_capture.py",
    "bytes": 11835,
    "sha256": "7758abc0e123e51f8f871dce060796347e63e2858759a58a7078fa669309ea40",
}
RAW_MAGIC = b"MDTEACHER-RAW-V3\n"
PROJECTION_MAGIC = b'{"contract":"multidetect.teacher-projection-journal-v3"}\n'
CONTRACT = "multidetect.onboard-teacher-observation-v3-file-audit-v1"


def check(condition, message):
    if not condition:
        raise ValueError(message)


def pairs(entries):
    result = {}
    for key, value in entries:
        check(key not in result, f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def loads(raw):
    def bad(value):
        raise ValueError(f"Nonfinite JSON token: {value}")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=bad)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )


def equal(left, right, message):
    check(canonical(left) == canonical(right), message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def flags(value):
    check(type(value) is dict and bool(value), "Nonempty claims required")
    check(all(item is False for item in value.values()), "A claim was promoted")


def integer(value, minimum=0):
    check(type(value) is int and value >= minimum, "Strict nonnegative integer required")
    return value


def real(value):
    check(type(value) in (int, float) and math.isfinite(value), "Finite number required")
    return value


class Reader:
    def __init__(self):
        self.bindings = {}

    def path(self, value):
        path = Path(value)
        if not path.is_absolute():
            path = ROOT / path
        check(not path.is_symlink(), "Symlink input refused")
        path = path.resolve(strict=True)
        path.relative_to(ROOT)
        check(path.is_file(), "Regular repository input required")
        return path

    def read(self, binding, *, cap=64 * 1024 * 1024, retain=True):
        path = self.path(binding["path"])
        size = integer(binding["bytes"])
        check(size <= cap, "Input byte cap exceeded")
        digest = binding["sha256"]
        check(type(digest) is str and re.fullmatch(r"[0-9a-f]{64}", digest), "SHA required")
        before = path.stat()
        check(before.st_size == size, f"Size differs: {path}")
        hashed, chunks = hashlib.sha256(), []
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                hashed.update(chunk)
                if retain:
                    chunks.append(chunk)
        after = path.stat()
        check(
            (before.st_size, before.st_mtime_ns, before.st_dev, before.st_ino)
            == (after.st_size, after.st_mtime_ns, after.st_dev, after.st_ino),
            "Input changed",
        )
        check(hashed.hexdigest() == digest, f"SHA differs: {path}")
        normalized = {"path": path.as_posix(), "bytes": size, "sha256": digest}
        prior = self.bindings.get(normalized["path"])
        check(prior is None or prior == normalized, "Conflicting current binding")
        self.bindings[normalized["path"]] = normalized
        return b"".join(chunks) if retain else None

    def declared(self, path, digest):
        resolved = self.path(path)
        binding = {"path": resolved.as_posix(), "bytes": resolved.stat().st_size, "sha256": digest}
        return binding, loads(self.read(binding))


def audit(observation_path, observation_sha, supervisor_path, supervisor_sha):
    reader = Reader()
    observation_binding, observation = reader.declared(observation_path, observation_sha)
    supervisor_binding, supervisor = reader.declared(supervisor_path, supervisor_sha)
    directory = Path(observation_binding["path"]).parent
    check(
        observation["contract"] == "multidetect.onboard-teacher-gazebo-observation-v3",
        "Observation contract differs",
    )
    check(
        observation["status"] in {"context-refused", "observed-context-qualified"},
        "Observation is not closed",
    )
    check(
        observation["error"] is None
        and observation["capture_close_error"] is None
        and observation["session_report_error"] is None,
        "Observation close failed",
    )
    child = supervisor["child"]
    check(
        type(child["native_exit_code"]) is int and child["native_exit_code"] in (0, 1),
        "Terminal native exit required",
    )
    check(
        child["native_exit_code"] == (0 if observation["context_qualified"] else 1),
        "Context and native outcome differ",
    )
    for name in (
        "owned_tree_empty",
        "capture_threads_closed",
        "launcher_started_suspended",
        "ownership_assigned_before_release",
        "worker_admitted_before_release",
    ):
        check(child[name] is True, f"Ownership/cleanup differs: {name}")
    for name in ("watchdog_fired", "deadline_exhausted", "capture_overflow"):
        check(child[name] is False, f"Unexpected supervisor failure: {name}")
    identity = child["worker_provenance"]["identity"]
    check(
        identity["pid"] == child["worker_pid"] and identity["ppid"] == child["launcher_pid"],
        "Worker parent identity differs",
    )
    check(
        child["worker_provenance"]["actual_parent_pid"] == identity["ppid"]
        and child["worker_provenance"]["job_membership_verified"] is True,
        "Worker Job differs",
    )
    argv = identity["argv"]
    check(
        Path(argv[0]).resolve() == ROOT / "scripts/run_onboard_teacher_gazebo_observation_v3.py"
        and "--execute" in argv
        and Path(argv[argv.index("--output-directory") + 1]).resolve() == directory,
        "Worker argv differs",
    )
    equal(supervisor["sources_before"], supervisor["sources_after"], "Source closure differs")
    check(supervisor["sources_unchanged"] is True, "Sources not closed")
    for binding in supervisor["sources_before"] + child["captures"]:
        reader.read(binding, retain=False)
    for name in (
        "final_release_allowed",
        "flight_commands_authorized",
        "model_or_optimizer_performed",
    ):
        check(supervisor[name] is False, f"Supervisor claim differs: {name}")
    flags(observation["claims"])
    check(
        observation["closed_snapshot_is_live_context"] is False
        and observation["injected_dependencies_used"] is False,
        "Observation boundary differs",
    )
    check(
        observation["clock"]["accuracy_verified"] is False
        and observation["clock"]["legacy_monotonic_equivalent"] is False,
        "Clock overstated",
    )
    capture = observation["raw_telemetry"]
    capture_file = loads(reader.read(capture["receipt"]))
    equal(capture_file, {k: v for k, v in capture.items() if k != "receipt"}, "Receipt differs")
    check(
        capture["status"] == "recorded"
        and capture["error"] is None
        and capture["evidence_error"] is None
        and capture["close_errors"] == [],
        "Capture not closed",
    )
    flags(capture["claims"])
    check(
        capture["owns_socket"] is False
        and capture["raw_commit_before_decoded_access"] is True
        and capture["dual_commit_before_cache"] is True,
        "Raw-first declaration differs",
    )
    for binding in (CODEC_BASE, CODEC):
        reader.read(binding)
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]
    codec = importlib.import_module("scripts.onboard_teacher_telemetry_capture_v2")
    check(Path(codec.__file__).resolve() == ROOT / CODEC["path"], "Codec import origin differs")
    journals = {
        stage: reader.read(binding) for stage, binding in capture["journal_sources"].items()
    }
    check(
        journals["raw"].startswith(RAW_MAGIC)
        and journals["projection"].startswith(PROJECTION_MAGIC),
        "Journal header differs",
    )
    for stage, binding in capture["journal_sources"].items():
        check(
            binding["verified_prefix_bytes"] == binding["bytes"]
            and binding["verified_prefix_sha256"] == binding["sha256"]
            and binding["verified_prefix_matches"] is True
            and binding["last_verified_file_identity_matches"] is True
            and binding["uncommitted_tail_bytes"] == 0,
            f"Incomplete journal: {stage}",
        )

    def sliced(stage, binding):
        check(
            Path(binding["path"]).resolve() == Path(capture["journal_sources"][stage]["path"]),
            "Slice path differs",
        )
        offset, size = integer(binding["offset"]), integer(binding["bytes"], 1)
        check(offset + size <= len(journals[stage]), "Slice outside journal")
        value = journals[stage][offset : offset + size]
        check(sha(value) == binding["sha256"], "Slice SHA differs")
        return value

    records, rows, decoded = capture["records"], [], []
    check(
        len(records) == capture["record_count"] == capture["packet_attempt_count"]
        and 1 <= len(records) <= 8192
        and capture["orphan_packet_count"] == 0,
        "Counts differ",
    )
    check(len(capture["packet_attempts"]) == len(records), "Attempt count differs")
    offsets = {"raw": len(RAW_MAGIC), "projection": len(PROJECTION_MAGIC)}
    packet_total, wire_matches, unknown_wire = 0, 0, 0
    type_counts, accepted_counts = Counter(), Counter()
    diagnostics = {
        "battery": [],
        "heartbeat": [],
        "vfr_nonfinite_airspeed": [],
        "command_ack": [],
        "bad_data": [],
        "unknown": [],
    }
    previous_received = -1
    for index, record in enumerate(records):
        check(
            record["record_index"] == index and record["status"] == "dual-committed",
            "Record order/status differs",
        )
        equal(
            capture["packet_attempts"][index],
            {
                k: v
                for k, v in record.items()
                if k not in {"raw_committed_at_s", "projection_committed_at_s"}
            },
            "Attempt differs",
        )
        for stage in offsets:
            binding = record[f"{stage}_record"]
            check(binding["offset"] == offsets[stage], "Noncontiguous journal records")
            offsets[stage] += binding["bytes"]
        raw = sliced("raw", record["raw_record"])
        header_size = struct.unpack("<I", raw[:4])[0]
        header = loads(raw[4 : 4 + header_size])
        packet = raw[4 + header_size :]
        equal(
            header,
            {
                "contract": capture["contract"],
                "record_index": index,
                "received_at_s": record["received_at_s"],
                "packet_bytes": record["packet_bytes"],
                "packet_sha256": record["packet_sha256"],
                "status": "raw-not-written",
                "message_type": None,
                "source_accepted": None,
            },
            "Raw header differs",
        )
        check(
            record["raw_record"]["packet_offset"]
            == record["raw_record"]["offset"] + 4 + header_size
            and len(packet) == record["packet_bytes"] == record["raw_record"]["packet_bytes"]
            and 1 <= len(packet) <= 4096
            and sha(packet) == record["packet_sha256"] == record["raw_record"]["packet_sha256"],
            "Raw packet differs",
        )
        packet_total += len(packet)
        received = real(record["received_at_s"])
        check(
            previous_received
            <= received
            <= record["raw_committed_at_s"]
            <= record["projection_committed_at_s"],
            "Commit/receive chronology differs",
        )
        previous_received = received
        projection = loads(sliced("projection", record["projection_record"]))
        check(projection["encoding"] == codec.ENCODING, "Typed encoding differs")
        equal(projection["raw_record"], record["raw_record"], "Projection raw binding differs")
        fields = codec.decode_typed_value(projection["typed_value"])
        equal(
            codec.encode_typed_value(fields), projection["typed_value"], "Typed roundtrip differs"
        )
        row = projection["row"]
        flags(row["claims"])
        for name in ("record_index", "received_at_s", "packet_sha256", "source_accepted"):
            equal(row[name], record[name], f"Projection identity differs: {name}")
        for name in ("clock_id", "transaction_id", "local_origin_id"):
            check(row[name] == capture[name], f"Source identity differs: {name}")
        kind = fields["mavpackettype"]
        expected_message = {
            **codec._project(projection["typed_value"]),
            "message_type": kind,
            "source_system_id": row["system_id"],
            "source_component_id": row["component_id"],
        }
        equal(row["message"], expected_message, "Compatible projection differs")
        check(
            record["message_type"] == kind
            and row["source_accepted"]
            is (
                (row["system_id"], row["component_id"])
                == (capture["expected_system_id"], capture["expected_component_id"])
            ),
            "Source gate differs",
        )
        if kind == "BAD_DATA" or kind.startswith("UNKNOWN"):
            unknown_wire += 1
        else:
            check(packet[0] in (253, 254), "Non-MAVLink packet with normal decoded kind")
            wire = (
                (packet[5], packet[6], packet[4])
                if packet[0] == 253
                else (packet[3], packet[4], packet[2])
            )
            check(
                wire == (row["system_id"], row["component_id"], row["packet_sequence"]),
                "Normal packet source/header differs",
            )
            wire_matches += 1
        type_counts[kind] += 1
        if row["source_accepted"]:
            accepted_counts[kind] += 1
        common = {
            "record_index": index,
            "received_at_s": received,
            "source_accepted": row["source_accepted"],
        }
        if kind == "BATTERY_STATUS":
            diagnostics["battery"].append(
                {
                    **common,
                    **{
                        k: fields[k]
                        for k in ("id", "time_remaining", "current_battery", "battery_remaining")
                    },
                    "time_remaining_type": type(fields["time_remaining"]).__name__,
                }
            )
        if kind == "HEARTBEAT":
            diagnostics["heartbeat"].append(
                {
                    **common,
                    **{k: fields[k] for k in ("type", "autopilot", "base_mode", "system_status")},
                }
            )
        if kind == "VFR_HUD" and not math.isfinite(fields["airspeed"]):
            diagnostics["vfr_nonfinite_airspeed"].append(
                {**common, "binary64_hex": struct.pack(">d", fields["airspeed"]).hex()}
            )
        if kind == "COMMAND_ACK":
            diagnostics["command_ack"].append(
                {**common, "fields": codec._project(projection["typed_value"])}
            )
        if kind == "BAD_DATA" or kind.startswith("UNKNOWN"):
            diagnostics["bad_data" if kind == "BAD_DATA" else "unknown"].append(
                {
                    **common,
                    "kind": kind,
                    "data_type": type(fields.get("data")).__name__,
                    "packet_bytes": len(packet),
                    "packet_sha256": sha(packet),
                }
            )
        rows.append(row)
        decoded.append(fields)
    check(all(offsets[s] == len(journals[s]) for s in offsets), "Journal has unaccounted tail")
    check(
        sum(r["source_accepted"] is True for r in rows) == capture["accepted_source_count"]
        and sum(r["source_accepted"] is False for r in rows) == capture["foreign_source_count"],
        "Source counts differ",
    )
    next_index = 0
    for index, batch in enumerate(capture["batches"]):
        ids = batch["record_indices"]
        check(
            batch["batch_index"] == index
            and batch["status"] == "dual-committed"
            and batch["error"] is None
            and 1 <= len(ids) <= 16
            and ids == list(range(next_index, next_index + len(ids))),
            "Batch partition differs",
        )
        next_index += len(ids)
        check(
            max(records[i]["received_at_s"] for i in ids)
            <= batch["started_at_s"]
            <= batch["raw_committed_at_s"]
            <= batch["projection_committed_at_s"],
            "Batch chronology",
        )
        for stage in ("raw", "projection"):
            check(
                batch[stage]["fsync_returned"] is True and batch[stage]["verified"] is True,
                "Batch not durable",
            )
            part = batch[stage]["slice"]
            check(
                part["offset"] == records[ids[0]][f"{stage}_record"]["offset"]
                and part["bytes"] == sum(records[i][f"{stage}_record"]["bytes"] for i in ids),
                "Batch slice does not cover records",
            )
            sliced(stage, part)
        for i in ids:
            for stage in ("raw", "projection"):
                check(
                    records[i][f"{stage}_committed_at_s"] == batch[f"{stage}_committed_at_s"],
                    "Per-record batch commit differs",
                )
        equal(
            batch["durations_s"],
            {
                "raw": batch["raw_committed_at_s"] - batch["started_at_s"],
                "projection": batch["projection_committed_at_s"] - batch["raw_committed_at_s"],
                "total": batch["projection_committed_at_s"] - batch["started_at_s"],
            },
            "Batch duration differs",
        )
    check(next_index == len(records), "Batch coverage incomplete")
    for counter in capture["commit_counts"].values():
        equal(
            counter,
            {k: len(capture["batches"]) for k in ("attempted", "fsync_returned", "verified")},
            "Commit counts differ",
        )

    by_kind = {}
    for record, row in zip(records, rows, strict=True):
        if row["source_accepted"]:
            by_kind.setdefault(row["message"]["message_type"], []).append(record)

    def latest(kind, at, maximum_age=2):
        candidates = by_kind.get(kind, [])
        position = bisect.bisect_right([r["projection_committed_at_s"] for r in candidates], at) - 1
        if position < 0:
            return None
        record = candidates[position]
        return record if 0 <= at - record["received_at_s"] <= maximum_age else None

    attempts = observation["qualification_attempts"]
    snapshot_indices, snapshot_bytes = set(), 0
    for snapshot in capture["snapshots"]:
        check(
            snapshot["status"] == "materialized" and snapshot["error"] is None,
            "Snapshot not fully materialized",
        )
        manifest = loads(reader.read(snapshot["manifest"]))
        snapshot_bytes += snapshot["manifest"]["bytes"]
        for name in ("clock_id", "transaction_id", "local_origin_id"):
            check(manifest[name] == capture[name], "Snapshot source identity differs")
        flags(manifest["claims"])
        check(
            manifest["received_times_renewed"] is False
            and manifest["started_at_s"]
            <= manifest["materialized_at_s"]
            <= snapshot["finished_at_s"],
            "Snapshot chronology/renewal differs",
        )
        check(
            set(manifest["rows"]) == set(KINDS) == set(manifest["selected"]),
            "Snapshot kinds differ",
        )
        for stage, binding in manifest["journal_sources"].items():
            check(
                binding["path"] == capture["journal_sources"][stage]["path"]
                and 0 < binding["bytes"] <= len(journals[stage])
                and sha(journals[stage][: binding["bytes"]])
                == binding["sha256"]
                == binding["verified_prefix_sha256"]
                and binding["verified_prefix_bytes"] == binding["bytes"]
                and binding["uncommitted_tail_bytes"] == 0
                and binding["verified_prefix_matches"] is True
                and binding["last_verified_file_identity_matches"] is True,
                "Historical prefix differs",
            )
        files = []
        attempt_index = int(Path(snapshot["directory"]).name.removeprefix("snapshot-"))
        snapshot_indices.add(attempt_index)
        attempt = attempts[attempt_index]
        for kind in KINDS:
            exported = manifest["rows"][kind]
            record_index = exported["record_index"]
            record, row = records[record_index], rows[record_index]
            check(latest(kind, attempt["observed_at_s"]) == record, "Snapshot not actual latest")
            equal(
                manifest["selected"][kind],
                {
                    k: record[k]
                    for k in (
                        "record_index",
                        "received_at_s",
                        "raw_record",
                        "projection_record",
                        "raw_committed_at_s",
                        "projection_committed_at_s",
                    )
                },
                "Snapshot selected pointer differs",
            )
            equal(
                {
                    k: v
                    for k, v in exported.items()
                    if k not in ("packet", "envelope", "dispatch_ready_at_s")
                },
                row,
                "Snapshot row differs",
            )
            packet = reader.read(exported["packet"])
            raw_record = record["raw_record"]
            check(
                packet
                == journals["raw"][
                    raw_record["packet_offset"] : raw_record["packet_offset"]
                    + raw_record["packet_bytes"]
                ],
                "Snapshot packet differs",
            )
            equal(loads(reader.read(exported["envelope"])), row, "Snapshot envelope differs")
            check(
                exported["dispatch_ready_at_s"] == manifest["materialized_at_s"]
                and 0 <= snapshot["finished_at_s"] - row["received_at_s"] <= 2,
                "Snapshot was stale",
            )
            files.extend((exported["packet"], exported["envelope"]))
        equal(files, snapshot["files"], "Snapshot inventory/order differs")
        snapshot_bytes += sum(binding["bytes"] for binding in files)
        expected_files = {
            Path(binding["path"]).resolve() for binding in files + [snapshot["manifest"]]
        }
        check(set(Path(snapshot["directory"]).iterdir()) == expected_files, "Snapshot extra files")
    camera_bindings, camera_times, camera_bytes = [], [], 0
    equal(
        observation["camera"],
        [r["camera"] for r in observation["camera_frames"]],
        "Camera list differs",
    )
    check(
        observation["camera_frames_recorded"] == len(observation["camera"]) == 6,
        "R3 six cameras required",
    )
    for index, frame in enumerate(observation["camera_frames"]):
        camera = loads(reader.read(frame["camera"]))
        flags(camera["claims"])
        check(
            frame["window_index"] == index // 2 and frame["frame_index"] == index % 2,
            "Camera windows/order differ",
        )
        provenance, depth = camera["provenance"], camera["depth"]
        host_at = camera["host_return_stamp"]["value_ns"] / 1e9
        check(
            host_at == frame["host_return_at_s"]
            and camera["host_return_stamp"]["session_id"] == capture["clock_id"]
            and frame["source_sensor_stamp_ns"] == provenance["sensor_stamp_ns"],
            "Camera clock/source binding differs",
        )
        camera_times.append(host_at)
        check(
            camera["transcoded"] is False
            and camera["px4_quality_qualification_claimed"] is False
            and camera["source_host_epoch_equivalence_claimed"] is False,
            "Camera claims differ",
        )
        check(
            (
                provenance["width"],
                provenance["height"],
                provenance["stride"],
                provenance["pixel_format"],
            )
            == (1280, 960, 3840, "BGR24")
            and (depth["width"], depth["height"], depth["stride_bytes"], depth["encoding"])
            == (1280, 960, 5120, "FLOAT32_M"),
            "Camera geometry differs",
        )
        rgb_binding, depth_binding = (
            camera["persisted_payloads"][name] for name in ("rgb-bgr24", "depth")
        )
        rgb = reader.read(rgb_binding)
        reader.read(depth_binding, retain=False)
        check(
            len(rgb) == 3686400 == camera["rgb_bgr24_bytes"] == provenance["payload_bytes"]
            and sha(rgb) == camera["rgb_bgr24_sha256"] == provenance["payload_sha256"],
            "RGB bytes differ",
        )
        check(
            depth_binding["bytes"] == 4915200 == depth["payload_bytes"]
            and depth_binding["sha256"]
            == depth["payload_sha256"]
            == provenance["depth_payload_sha256"],
            "Depth bytes differ",
        )
        check(
            sha(bytes.fromhex(provenance["wire_unsigned_header_hex"]) + rgb)
            == provenance["record_sha256"],
            "RGB wire record identity differs",
        )
        for stamp, skew in (
            ("navsat_stamp_ns", "navsat_source_skew_ns"),
            ("odometry_stamp_ns", "odometry_source_skew_ns"),
            ("camera_info_stamp_ns", "camera_info_source_skew_ns"),
            ("depth_source_stamp_ns", "depth_source_skew_ns"),
        ):
            check(
                provenance["sensor_stamp_ns"] - provenance[stamp] == provenance[skew] >= 0,
                "Same-sensor epoch skew differs",
            )
        camera_bindings.extend((frame["camera"], rgb_binding, depth_binding))
        camera_bytes += rgb_binding["bytes"] + depth_binding["bytes"]
    check(camera_times == sorted(camera_times), "Camera host receive chronology differs")
    for index, attempt in enumerate(attempts):
        check(
            attempt["attempt_index"] == index
            and attempt["qualified"] is False
            and attempt["camera_age_is_sensor_clock_mapping"] is False,
            "Attempt identity/claim differs",
        )
        at = real(attempt["observed_at_s"])
        present = {kind: latest(kind, at) for kind in KINDS}
        equal(
            attempt["present"],
            sorted(kind for kind, row in present.items() if row),
            "Present kinds differ",
        )
        equal(
            attempt["receive_ages_s"],
            {
                kind: None if row is None else at - row["received_at_s"]
                for kind, row in present.items()
            },
            "Receive ages renewed or changed",
        )
        visible_cameras = [value for value in camera_times if value <= at]
        age = None if not visible_cameras else at - visible_cameras[-1]
        equal(attempt["camera_host_return_age_s"], age, "Camera age differs")
        if all(present.values()) and visible_cameras:
            times = [row["received_at_s"] for row in present.values()]
            equal(attempt["receive_skew_s"], max(times) - min(times), "Receive skew differs")
            check(index in snapshot_indices, "Complete attempt missing snapshot")
            if max(times) - min(times) > 1:
                check(
                    attempt["error"] == "ValueError: required telemetry observer-time skew "
                    "exceeds qualification bound",
                    "Stored skew refusal differs from actual selected receive times",
                )
            else:
                heartbeat = decoded[present["HEARTBEAT"]["record_index"]]
                check(
                    heartbeat["system_status"] == 0
                    and attempt["error"]
                    == "ValueError: context qualification requires actual STANDBY HEARTBEAT",
                    "Stored heartbeat refusal differs from actual selected heartbeat",
                )
        else:
            check(
                attempt["receive_skew_s"] is None and index not in snapshot_indices,
                "Incomplete attempt has invented snapshot/skew",
            )
    check(
        sum(map(len, journals.values())) + snapshot_bytes
        == capture["verified_durable_data_bytes"]
        == capture["data_budget_charged_bytes"]
        <= 64 * 1024 * 1024,
        "Materialized data budget accounting differs",
    )
    receiver, session = observation["receiver"], observation["session"]
    equal(receiver, session["receiver"], "Receiver report differs")
    check(
        receiver["received_packets"] == receiver["consumed_packets"] == len(records)
        and receiver["raw_packet_bytes"] == packet_total
        and receiver["queued_packets"] == 0
        and receiver["queue_overflow"] is False
        and receiver["failure"] is None
        and receiver["thread_alive"] is False
        and receiver["thread_exited"] is True,
        "Receiver close/queue/accounting differs",
    )
    check(
        session["status"] == "closed"
        and session["cleanup_verified"] is True
        and session["container_absent_after"] is True
        and session["error"] is None
        and session["receive_error"] is None
        and session["cleanup_errors"] == []
        and session["camera_thread_alive"] is False
        and session["flight_authority"] is False,
        "Session cleanup differs",
    )
    check(
        all(value is True for value in session["ownership_checks"].values()),
        "Ownership gate differs",
    )
    windows = session["camera_windows"]
    check(
        windows["complete"] is True
        and windows["failure"] is None
        and windows["cleanup_errors"] == []
        and len(windows["windows"]) == 3,
        "Camera cleanup differs",
    )
    for window in windows["windows"]:
        check(
            window["complete"] is True
            and window["frames"] == 2
            and window["native_stats"]["helper_exit_code"] == 0,
            "Camera helper terminal differs",
        )
    check(
        receiver["request_API_calls"] == observation["telemetry_stream_request_API_calls"] == 11,
        "Stream request API accounting differs",
    )
    stream_requests = loads(reader.read(observation["stream_requests"]))
    flags(stream_requests["claims"])
    equal(
        stream_requests["requests"],
        session["stream_request_API_attempts"],
        "Saved stream requests differ",
    )
    for request in stream_requests["requests"]:
        check(
            request["API_returned"] is True
            and request["error"] is None
            and request["acknowledged"] is False
            and request["datagram_delivery_independently_verified"] is False
            and request["clock_id"] == capture["clock_id"],
            "Stream API boundary differs",
        )
    reader.read(observation["runtime_source"])
    plan_path = directory / "plan.json"
    plan_raw = plan_path.read_bytes()
    plan_binding = {"path": plan_path.as_posix(), "bytes": len(plan_raw), "sha256": sha(plan_raw)}
    equal(loads(reader.read(plan_binding)), observation["plan"], "Saved plan differs")
    for binding in supervisor["sources_before"]:
        reader.read(binding, retain=False)
    reader.read(observation_binding, retain=False)
    reader.read(supervisor_binding, retain=False)
    flags(observation["claims"])
    for binding in (CODEC_BASE, CODEC):
        reader.read(binding)
    check(
        not any(
            name == root or name.startswith(root + ".")
            for name in sys.modules
            for root in ("torch", "transformers", "peft", "PIL", "numpy", "pymavlink")
        ),
        "Heavy import occurred",
    )
    bindings = sorted(reader.bindings.values(), key=lambda item: item["path"])
    summary = {
        "contract": CONTRACT,
        "schema_version": 1,
        "audit_passed": True,
        "observation": observation_binding,
        "supervisor": supervisor_binding,
        "capture_receipt": capture["receipt"],
        "journal_sources": capture["journal_sources"],
        "source_bindings_current_verified": supervisor["sources_before"],
        "audit_file_count": len(bindings),
        "audit_file_bytes": sum(b["bytes"] for b in bindings),
        "audit_file_inventory_sha256": sha(canonical(bindings).encode("utf-8")),
        "record_count": len(records),
        "accepted_source_count": capture["accepted_source_count"],
        "foreign_source_count": capture["foreign_source_count"],
        "orphan_packet_count": 0,
        "batch_count": len(capture["batches"]),
        "commit_counts": capture["commit_counts"],
        "all_record_and_batch_slices_verified": True,
        "typed_roundtrip_count": len(records),
        "normal_wire_header_source_matches": wire_matches,
        "unknown_or_BAD_DATA_wire_not_relabelled": unknown_wire,
        "message_counts": dict(type_counts),
        "accepted_message_counts": dict(accepted_counts),
        "snapshot_count": len(capture["snapshots"]),
        "snapshot_packet_and_envelope_bindings": len(capture["snapshots"]) * 18,
        "snapshot_original_receive_times_verified": True,
        "qualification_attempts": len(attempts),
        "qualification_error_counts": dict(Counter(attempt["error"] for attempt in attempts)),
        "receive_skew_s_range": [
            min(a["receive_skew_s"] for a in attempts if a["receive_skew_s"] is not None),
            max(a["receive_skew_s"] for a in attempts if a["receive_skew_s"] is not None),
        ],
        "camera_bindings": camera_bindings,
        "camera_frames": 6,
        "original_camera_payload_bytes": camera_bytes,
        "raw_payload_bytes": packet_total,
        "telemetry_data_budget_bytes": capture["verified_durable_data_bytes"],
        "receiver": receiver,
        "observation_status": observation["status"],
        "context_qualified": observation["context_qualified"],
        "last_context_error": observation["context_error"],
        "recorded_native_exit_code": child["native_exit_code"],
        "recorded_worker_pid": child["worker_pid"],
        "recorded_launcher_pid": child["launcher_pid"],
        "native_exit_independently_reobserved": False,
        "diagnostics": diagnostics,
        "scope": "Saved file bytes/slices/typed projections/source inventory/chronology only; "
        "no decoder, PNG, Docker, processor, model, GPU, flight, new process/native-exit "
        "observation or source authentication",
        "limitations": [
            "Typed roundtrip is not independent to_dict or MAVLink CRC validation",
            "Depth payload SHA is checked; depth wire header was not independently reconstructed",
            "Host receive times do not certify sensor/PX4/Gazebo clocks or origin mapping",
            "Recorded fsync/ownership/native flags remain producer/supervisor observations",
            "R1 dropped packet type is not inferred from R3 records",
        ],
        "claims": {key: False for key in observation["claims"]},
    }
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observation", required=True, type=Path)
    parser.add_argument("--observation-sha256", required=True)
    parser.add_argument("--supervisor", required=True, type=Path)
    parser.add_argument("--supervisor-sha256", required=True)
    parser.add_argument("--audit-output", type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.audit_output.resolve()
    output.relative_to(ROOT / "artifacts/diagnostics")
    check(not output.exists() and output.suffix == ".json", "Fresh diagnostics audit JSON required")
    report = audit(
        args.observation, args.observation_sha256, args.supervisor, args.supervisor_sha256
    )
    source = Path(__file__).resolve()
    source_raw = source.read_bytes()
    report["audit_source"] = {
        "path": source.as_posix(),
        "bytes": len(source_raw),
        "sha256": sha(source_raw),
    }
    raw = (
        json.dumps(report, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    check(output.parent.is_dir(), "Existing fresh audit parent required")
    with output.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    print(
        json.dumps(
            {
                "audit_passed": True,
                "observation_status": report["observation_status"],
                "recorded_native_exit_code": report["recorded_native_exit_code"],
                "audit": {"path": output.as_posix(), "bytes": len(raw), "sha256": sha(raw)},
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
