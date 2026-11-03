"""CPU-only software checks; synthetic packets are not actual PX4 acceptance."""

import copy
import hashlib
import importlib.util
import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "event_diagnostics", REPO / "scripts/diagnose_onboard_teacher_px4_events.py"
)
api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(api)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def packet(message_id, payload, *, extra=None, flags=0, trim=True, sender=(1, 1)):
    if trim:
        payload = payload.rstrip(b"\x00") or b"\x00"
    header = bytes([len(payload), flags, 0, 4, *sender]) + message_id.to_bytes(3, "little")
    crc = api.crc16(
        header + payload + bytes([api.HEADERS[message_id][4] if extra is None else extra])
    )
    return b"\xfd" + header + payload + struct.pack("<H", crc)


def event_packet(event_id=0x0106ED29, *, args=b"\x01\x00\x00\x00\x00", sequence=10):
    return packet(
        410, struct.pack("<IIHBBB", event_id, 123, sequence, 0, 0, 0x44) + args.ljust(40, b"\x00")
    )


def metadata(arguments=None):
    return {
        "version": 2,
        "components": {
            "1": {
                "namespace": "px4",
                "enums": {},
                "event_groups": {
                    "arming_check": {
                        "events": {
                            "453929": {
                                "name": "check_modes_manual_control",
                                "message": "No manual control input",
                                "arguments": arguments
                                if arguments is not None
                                else [
                                    {"name": "modes", "type": "uint32_t"},
                                    {"name": "index", "type": "uint8_t"},
                                ],
                            }
                        }
                    }
                },
            }
        },
    }


class Fixture:
    def __init__(self, root, monkeypatch):
        self.root = root
        monkeypatch.setattr(api, "ROOT", root)
        self.capture_path, self.metadata_path, self.extraction_path = [
            root / x for x in ("capture.json", "events.json", "extraction.json")
        ]
        self.header_dir = root / "headers"
        self.header_dir.mkdir()
        for name, *_ in api.HEADERS.values():
            (self.header_dir / name).write_bytes(
                (
                    REPO
                    / "artifacts/diagnostics/onboard-teacher-px4-event-definitions-20261008-r1"
                    / name
                ).read_bytes()
            )
        (root / "tests").mkdir()
        (root / "tests/test_diagnose_onboard_teacher_px4_events.py").write_bytes(
            b"# synthetic source\n"
        )
        self.document = metadata()
        self.capture = {
            "contract": api.CAPTURE_CONTRACT,
            "status": "recorded",
            "error": None,
            "evidence_error": None,
            "records": [],
            "record_count": 0,
            "journal_sources": {},
        }
        raw, projections = bytearray(b"MDTEACHER-RAW-V3\n"), bytearray(b"header\n")
        for index, original in enumerate((packet(411, struct.pack("<HB", 9, 1)), event_packet())):
            digest = sha(original)
            header = api.canonical(
                {
                    "contract": api.CAPTURE_CONTRACT,
                    "record_index": index,
                    "received_at_s": 100.0 + index,
                    "packet_bytes": len(original),
                    "packet_sha256": digest,
                }
            )
            frame = struct.pack("<I", len(header)) + header + original
            rb = {
                "path": (root / "raw.journal").as_posix(),
                "offset": len(raw),
                "bytes": len(frame),
                "sha256": sha(frame),
                "packet_offset": len(raw) + 4 + len(header),
                "packet_bytes": len(original),
                "packet_sha256": digest,
            }
            kind = "UNKNOWN_411" if index == 0 else "UNKNOWN_410"
            envelope = {
                "record_index": index,
                "received_at_s": 100.0 + index,
                "source_accepted": False,
                "system_id": 0,
                "component_id": 0,
                "packet_sha256": digest,
                "message": {"message_type": kind},
            }
            projection = api.canonical({"raw_record": rb, "row": envelope})
            pb = {
                "path": (root / "projections.jsonl").as_posix(),
                "offset": len(projections),
                "bytes": len(projection),
                "sha256": sha(projection),
            }
            raw.extend(frame)
            projections.extend(projection)
            self.capture["records"].append(
                {
                    "record_index": index,
                    "received_at_s": 100.0 + index,
                    "source_accepted": False,
                    "status": "dual-committed",
                    "message_type": kind,
                    "raw_record": rb,
                    "projection_record": pb,
                    "packet_bytes": len(original),
                    "packet_sha256": digest,
                }
            )
        for role, name, contents in (
            ("raw", "raw.journal", raw),
            ("projection", "projections.jsonl", projections),
        ):
            (root / name).write_bytes(contents)
            self.capture["journal_sources"][role] = {
                "path": (root / name).as_posix(),
                "bytes": len(contents),
                "sha256": sha(contents),
                "verified_prefix_matches": True,
                "last_verified_file_identity_matches": True,
                "uncommitted_tail_bytes": 0,
            }
        self.capture["record_count"] = 2
        self.bind()

    def bind(self):
        self.metadata_path.write_bytes(api.canonical(self.document))
        self.extraction = {
            "contract": "multidetect.teacher-pinned-PX4-metadata-extraction-v1",
            "status": "pinned-image-metadata-extracted",
            "image": api.IMAGE,
            "container_started": False,
            "cleanup_verified": True,
            "error": None,
            "files": {
                "all_events.json.xz": {
                    "expanded": {
                        "path": self.metadata_path.as_posix(),
                        "bytes": self.metadata_path.stat().st_size,
                        "sha256": sha(self.metadata_path.read_bytes()),
                    }
                }
            },
        }
        self.extraction_path.write_bytes(api.canonical(self.extraction))
        self.capture_path.write_bytes(api.canonical(self.capture))

    def run(self, **kwargs):
        return api.diagnose(
            self.capture_path,
            sha(self.capture_path.read_bytes()),
            self.metadata_path,
            sha(self.metadata_path.read_bytes()),
            self.extraction_path,
            sha(self.extraction_path.read_bytes()),
            header_directory=self.header_dir,
            **kwargs,
        )


@pytest.fixture
def fixture(tmp_path, monkeypatch, event_headers):
    return Fixture(tmp_path, monkeypatch)


@pytest.fixture
def event_headers():
    directory = REPO / "artifacts/diagnostics/onboard-teacher-px4-event-definitions-20261008-r1"
    for name, *_ in api.HEADERS.values():
        if not (directory / name).is_file():
            pytest.skip(f"pinned PX4 event definition evidence is missing: {directory / name}")


def test_crc_golden_and_exact_header_bindings(event_headers):
    assert api.crc16(b"123456789") == 0x6F91
    for name, size, digest, *_ in api.HEADERS.values():
        raw = (
            REPO / "artifacts/diagnostics/onboard-teacher-px4-event-definitions-20261008-r1" / name
        ).read_bytes()
        assert (len(raw), sha(raw)) == (size, digest)


def test_current_zero_trim_after_crc():
    raw = packet(411, b"\x00\x00\x00")
    decoded = api.decode_packet(raw)
    assert decoded["wire_payload_bytes"] == 1
    assert decoded["zero_extended_bytes"] == 2
    assert decoded["sequence"] == 0 and not decoded["reset"]


def test_public_wire_api_does_not_zero_extend_or_assume_events():
    payload = b"\x01\x02\x00"
    header = bytes([3, 0, 0, 7, 1, 2]) + (22).to_bytes(3, "little")
    raw = b"\xfd" + header + payload + struct.pack("<H", api.crc16(header + payload + b"\xdc"))
    result = api.validate_mavlink2_frame(
        raw, expected_message_id=22, crc_extra=220, max_payload_length=25
    )
    assert result["payload_raw"] == payload
    assert result["wire_component_id"] == 2 and result["crc_verified"]
    with pytest.raises(api.DiagnosticError, match="message ID"):
        api.validate_mavlink2_frame(
            raw, expected_message_id=148, crc_extra=220, max_payload_length=25
        )


@pytest.mark.parametrize(
    "mutation", ["crc", "extra", "length", "trailing", "magic", "signed", "unknownflag", "oversize"]
)
def test_wire_rejects_corruption(mutation):
    raw = bytearray(event_packet())
    if mutation == "crc":
        raw[-1] ^= 1
    elif mutation == "extra":
        raw = bytearray(packet(410, b"\x00" * 53, extra=159))
    elif mutation == "length":
        raw[1] += 1
    elif mutation == "trailing":
        raw += b"\x00"
    elif mutation == "magic":
        raw[0] = 0xFE
    elif mutation in ("signed", "unknownflag"):
        raw[2] = 1 if mutation == "signed" else 2
    else:
        raw = bytearray(packet(410, b"X" * 54, trim=False))
    with pytest.raises(api.DiagnosticError):
        api.decode_packet(bytes(raw))


@pytest.mark.parametrize(
    "type_name,value",
    [
        ("uint8_t", 255),
        ("int8_t", -128),
        ("uint16_t", 65535),
        ("int16_t", -32768),
        ("uint32_t", 2**32 - 1),
        ("int32_t", -(2**31)),
        ("uint64_t", 2**64 - 1),
        ("int64_t", -(2**63)),
        ("float", -3.5),
    ],
)
def test_all_nine_argument_types(type_name, value):
    events, enums = api.validate_metadata(metadata([{"name": "arg", "type": type_name}]))
    raw = struct.pack("<" + api.TYPES[type_name], value).ljust(40, b"\x00")
    decoded = api.decode_arguments(0x0106ED29, raw, events, enums)
    assert decoded["arguments"][0]["value"] == value
    assert decoded["argument_bytes_used"] == struct.calcsize("<" + api.TYPES[type_name])


@pytest.mark.parametrize("bits", ["0000807f", "000080ff", "0100c07f", "00000080"])
def test_float_bits_preserved_including_nonfinite_and_negative_zero(bits):
    events, enums = api.validate_metadata(metadata([{"name": "f", "type": "float"}]))
    value = api.decode_arguments(0x0106ED29, bytes.fromhex(bits).ljust(40, b"\x00"), events, enums)[
        "arguments"
    ][0]
    assert value["ieee754_f32_bits"] == bits
    if bits != "00000080":
        assert value["value"] is None and value["finite"] is False
    else:
        assert struct.pack("<f", value["value"]).hex() == bits and value["finite"]
    api.canonical(value)


def test_cross_namespace_enum_unknown_bits_and_unknown_scalar():
    doc = metadata([{"name": "bits", "type": "common::bits_t"}, {"name": "code", "type": "code_t"}])
    doc["components"]["0"] = {
        "namespace": "common",
        "enums": {
            "bits_t": {
                "type": "uint8_t",
                "is_bitfield": True,
                "entries": {"1": {"name": "one", "description": "one"}},
            }
        },
    }
    doc["components"]["1"]["enums"]["code_t"] = {
        "type": "int8_t",
        "entries": {"1": {"name": "yes"}},
    }
    events, enums = api.validate_metadata(doc)
    values = api.decode_arguments(0x0106ED29, b"\x05\xff".ljust(40, b"\x00"), events, enums)[
        "arguments"
    ]
    assert values[0]["enum_labels"] == ["one"] and values[0]["unknown_bits"] == 4
    assert values[1]["value"] == -1 and values[1]["enum_name"] is None


def test_namespace_not_wire_sender_and_unknown_metadata_preserved():
    decoded = api.decode_packet(event_packet(event_id=0x02000003))
    assert decoded["namespace_component_id"] == 2 and decoded["wire_component_id"] == 1
    events, enums = api.validate_metadata(metadata())
    assert (
        api.decode_arguments(
            decoded["event_id"], bytes.fromhex(decoded["arguments_hex"]), events, enums
        )["metadata_matched"]
        is False
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "boolversion",
        "version",
        "component",
        "duplicate",
        "unknown_type",
        "bytes",
        "bitbool",
        "extra",
        "argextra",
        "enumrange",
    ],
)
def test_metadata_schema_and_unique_ids(mutation):
    doc = metadata()
    comp = doc["components"]["1"]
    event = comp["event_groups"]["arming_check"]["events"]["453929"]
    if mutation == "boolversion":
        doc["version"] = True
    elif mutation == "version":
        doc["version"] = 3
    elif mutation == "component":
        doc["components"]["01"] = doc["components"].pop("1")
    elif mutation == "duplicate":
        comp["event_groups"]["other"] = copy.deepcopy(comp["event_groups"]["arming_check"])
    elif mutation == "unknown_type":
        event["arguments"][0]["type"] = "double"
    elif mutation == "bytes":
        event["arguments"] = [{"name": str(i), "type": "uint64_t"} for i in range(6)]
    elif mutation == "bitbool":
        comp["enums"]["bits_t"] = {"type": "uint8_t", "is_bitfield": 1}
    elif mutation == "extra":
        event["unbound"] = "x"
    elif mutation == "argextra":
        event["arguments"][0]["unbound"] = "x"
    else:
        comp["enums"]["bad_t"] = {"type": "uint8_t", "entries": {"256": {"name": "overflow"}}}
    with pytest.raises(api.DiagnosticError):
        api.validate_metadata(doc)


@pytest.mark.parametrize(
    "raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}', b"\xff", b"[" * 34 + b"0" + b"]" * 34]
)
def test_strict_json_rejects_duplicates_nonfinite_utf8_and_depth(raw):
    with pytest.raises(api.DiagnosticError):
        api.strict_json(raw)


def test_strict_json_hard_byte_cap():
    with pytest.raises(api.DiagnosticError, match="byte cap"):
        api.strict_json(b'{"long":1}', cap=3)


def test_sequence_wrap_reset_gap_half_range_and_distinct_sender():
    states = {}

    def take(seq, sender=1, message=410, reset=False):
        return api.sequence_observation(
            {
                "wire_system_id": 1,
                "wire_component_id": sender,
                "message_id": message,
                "sequence": seq,
                "reset": reset,
            },
            states,
        )

    assert take(65535)["status"] == "initial"
    assert take(0)["wrapped"] is True
    assert take(0)["status"] == "duplicate_or_current"
    assert take(3)["missing_count"] == 2
    assert take(32771)["status"] == "ambiguous_half_range"
    assert take(2)["status"] == "older_or_retransmitted"
    assert take(8, sender=2)["status"] == "initial"
    assert take(6, message=411)["status"] == "forward_gap" and states["1:1"] == 3
    assert take(10, message=411, reset=True)["status"] == "reset"


def test_full_closed_fixture_keeps_unknowns_and_diagnostic_only(fixture):
    report = fixture.run()
    assert report["success"] and report["sources_before"] == report["sources_after"]
    assert report["record_count"] == 2 and report["all_selected_crc_verified"]
    assert report["manual_control_input_event_count"] == 1
    assert not any(report["claims"].values())
    assert all(
        row["original_source_accepted"] is False and row["original_decoder_system_id"] == 0
        for row in report["rows"]
    )
    assert report["rows"][1]["metadata"]["message_template"] == "No manual control input"


@pytest.mark.parametrize(
    "mutation",
    [
        "accepted",
        "indexbool",
        "notclosed",
        "tail",
        "slice",
        "packetsha",
        "path",
        "recordcount",
        "capture_error",
    ],
)
def test_capture_binding_fail_closed(fixture, mutation):
    row = fixture.capture["records"][1]
    if mutation == "accepted":
        row["source_accepted"] = True
    elif mutation == "indexbool":
        row["record_index"] = True
    elif mutation == "notclosed":
        fixture.capture["status"] = "aborted"
    elif mutation == "tail":
        fixture.capture["journal_sources"]["raw"]["uncommitted_tail_bytes"] = 1
    elif mutation == "slice":
        row["raw_record"]["sha256"] = "0" * 64
    elif mutation == "packetsha":
        row["packet_sha256"] = "0" * 64
    elif mutation == "path":
        row["raw_record"]["path"] = (fixture.root.parent / "outside").as_posix()
    elif mutation == "recordcount":
        fixture.capture["record_count"] = True
    else:
        fixture.capture["error"] = {"message": "partial"}
    fixture.bind()
    with pytest.raises(api.DiagnosticError):
        fixture.run()


def test_packet_count_cap_before_decode(fixture):
    with pytest.raises(api.DiagnosticError, match="count cap"):
        fixture.run(max_packets=1)


def test_late_source_drift_rejects_result(fixture, monkeypatch):
    original, reads = api.read_bound, 0

    def drift(path, **kwargs):
        nonlocal reads
        if Path(path) == fixture.metadata_path:
            reads += 1
            if reads == 2:
                fixture.metadata_path.write_bytes(b"changed after decode")
        return original(path, **kwargs)

    monkeypatch.setattr(api, "read_bound", drift)
    with pytest.raises(api.DiagnosticError, match="SHA"):
        fixture.run()


def test_header_mutation_rejected_before_decode(fixture):
    (fixture.header_dir / api.HEADERS[410][0]).write_bytes(b"changed header")
    with pytest.raises(api.DiagnosticError, match="SHA"):
        fixture.run()


@pytest.mark.parametrize(
    "role", ["projection_kind", "projection_index", "projection_timestamp", "raw_header"]
)
def test_rebound_bytes_cannot_hide_row_identity_drift(fixture, role):
    row = fixture.capture["records"][1]
    journal_role = "raw" if role == "raw_header" else "projection"
    binding = row["raw_record"] if role == "raw_header" else row["projection_record"]
    path = Path(binding["path"])
    contents = path.read_bytes()
    segment = contents[binding["offset"] : binding["offset"] + binding["bytes"]]
    if role == "raw_header":
        size = struct.unpack("<I", segment[:4])[0]
        header = json.loads(segment[4 : 4 + size])
        header["record_index"] = 0
        encoded = api.canonical(header)
        replacement = struct.pack("<I", len(encoded)) + encoded + segment[4 + size :]
    else:
        projection = json.loads(segment)
        if role == "projection_kind":
            projection["row"]["message"]["message_type"] = "HEARTBEAT"
        elif role == "projection_index":
            projection["row"]["record_index"] = True
        else:
            projection["row"]["received_at_s"] = True
        replacement = api.canonical(projection)
    rewritten = contents[: binding["offset"]] + replacement
    binding.update(bytes=len(replacement), sha256=sha(replacement))
    path.write_bytes(rewritten)
    fixture.capture["journal_sources"][journal_role].update(
        bytes=len(rewritten), sha256=sha(rewritten)
    )
    fixture.bind()
    with pytest.raises(api.DiagnosticError):
        fixture.run()


def test_bad_crc_with_all_artifact_hashes_rebound_still_rejected(fixture):
    row = fixture.capture["records"][1]
    rb, pb = row["raw_record"], row["projection_record"]
    path = Path(rb["path"])
    journal = bytearray(path.read_bytes())
    journal[rb["packet_offset"] + rb["packet_bytes"] - 1] ^= 1
    # Rebind every outer SHA, so the only rejecting gate is actual wire CRC.
    original = bytes(journal[rb["packet_offset"] : rb["packet_offset"] + rb["packet_bytes"]])
    digest = sha(original)
    size = struct.unpack("<I", journal[rb["offset"] : rb["offset"] + 4])[0]
    header = json.loads(journal[rb["offset"] + 4 : rb["offset"] + 4 + size])
    header["packet_sha256"] = digest
    encoded = api.canonical(header)
    assert len(encoded) == size
    journal[rb["offset"] + 4 : rb["offset"] + 4 + size] = encoded
    rb.update(packet_sha256=digest, sha256=sha(journal[rb["offset"] : rb["offset"] + rb["bytes"]]))
    row["packet_sha256"] = digest
    path.write_bytes(journal)
    fixture.capture["journal_sources"]["raw"]["sha256"] = sha(journal)
    path = Path(pb["path"])
    all_projections = path.read_bytes()
    projection = json.loads(all_projections[pb["offset"] : pb["offset"] + pb["bytes"]])
    projection["raw_record"] = rb.copy()
    projection["row"]["packet_sha256"] = digest
    encoded = api.canonical(projection)
    all_projections = all_projections[: pb["offset"]] + encoded
    pb.update(bytes=len(encoded), sha256=sha(encoded))
    path.write_bytes(all_projections)
    fixture.capture["journal_sources"]["projection"].update(
        bytes=len(all_projections), sha256=sha(all_projections)
    )
    fixture.bind()
    with pytest.raises(api.DiagnosticError, match="CRC"):
        fixture.run()


def test_create_only_output_exact_bytes_and_failure_no_overwrite(fixture):
    output = fixture.root / "artifacts/diagnostics/new.json"
    report = fixture.run()
    binding = api.write_new(output, report)
    before = output.read_bytes()
    assert binding["sha256"] == sha(before)
    assert json.loads(before) == report
    with pytest.raises(api.DiagnosticError, match="already exists"):
        api.write_new(output, {"changed": True})
    assert output.read_bytes() == before
    with pytest.raises(api.DiagnosticError, match="new diagnostics"):
        api.write_new(fixture.root / "not-diagnostics.json", report)


def test_fsync_failure_keeps_partial_and_no_success_binding(fixture, monkeypatch):
    monkeypatch.setattr(api.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("fsync failed")))
    output = fixture.root / "artifacts/diagnostics/partial.json"
    with pytest.raises(OSError, match="fsync"):
        api.write_new(output, {"partial": True})
    assert output.exists()


def test_missing_actual_artifact_cli_refuses_without_output(tmp_path):
    output = REPO / "artifacts/diagnostics/unused-event-fixture-report.json"
    assert not output.exists()
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(REPO / "scripts/diagnose_onboard_teacher_px4_events.py"),
            "--capture-receipt",
            str(tmp_path / "missing"),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1 and "DiagnosticError" in result.stderr
    assert not output.exists()


def test_guarded_import_is_pure_stdlib():
    code = (
        "import runpy,sys;runpy.run_path("
        + repr(str(REPO / "scripts/diagnose_onboard_teacher_px4_events.py"))
        + ");assert not any(x in sys.modules for x in "
        "('torch','PIL','numpy','pymavlink','transformers'))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
