"""Original union bits/CRC/closed journal fixtures, not runtime PX4 evidence."""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import pytest

from scripts import diagnose_onboard_teacher_px4_parameters as diagnostics
from scripts.diagnose_onboard_teacher_px4_events import crc16
from scripts.onboard_teacher_telemetry_capture_v3 import TeacherTelemetryCaptureV3


def frame(message_id, payload, *, sequence=1, system=1, component=1):
    trimmed = payload.rstrip(b"\0") or b"\0"
    header = bytes((len(trimmed), 0, 0, sequence, system, component)) + message_id.to_bytes(
        3, "little"
    )
    extra = 220 if message_id == 22 else 178
    return b"\xfd" + header + trimmed + struct.pack("<H", crc16(header + trimmed + bytes((extra,))))


def param(raw, *, kind=6, name=b"SYS_AUTOSTART", count=2, index=0, **kwargs):
    return frame(22, raw + struct.pack("<HH16sB", count, index, name, kind), **kwargs)


def version(caps=16, *, sequence=1, firmware=123):
    return frame(
        148,
        struct.pack("<QQIIIIHH", caps, 0, firmware, 0, 0, 0, 0, 0) + bytes(42),
        sequence=sequence,
    )


@pytest.mark.parametrize("value", (4001, -1, 0, -(1 << 31), (1 << 31) - 1))
def test_bytewise_int32_never_float_cast(value):
    raw = struct.pack("<i", value)
    result = diagnostics.decode_response_packet(param(raw), parameter_encoding="bytewise")
    assert result["value"] == value
    assert result["raw_union_hex"] == raw.hex() and result["crc_verified"]
    assert result["authentication_verified"] is False


def test_missing_capabilities_does_not_fill_zero():
    result = diagnostics.decode_response_packet(param(bytes(4)))
    assert result["value"] is None and result["interpretation_status"] == "encoding-unproven"


@pytest.mark.parametrize(
    "capabilities,encoding", ((16, "bytewise"), (131072, "c_cast"), (0, None), (131088, None))
)
def test_encoding_comes_from_actual_capability_bits(capabilities, encoding):
    assert (
        diagnostics.decode_response_packet(version(capabilities))["parameter_encoding"] == encoding
    )


@pytest.mark.parametrize(
    "kind,encoding,raw",
    (
        (9, "bytewise", bytes.fromhex("0000807f")),
        (9, "c_cast", bytes.fromhex("0000c07f")),
        (6, "c_cast", bytes.fromhex("ffffffff")),
    ),
)
def test_nonfinite_original_union_preserved_not_numeric(kind, encoding, raw):
    result = diagnostics.decode_response_packet(param(raw, kind=kind), parameter_encoding=encoding)
    assert result["value"] is None and result["raw_union_hex"] == raw.hex()
    assert result["interpretation_status"] == "nonfinite-value-retained-as-raw-bits"


def test_c_cast_rejects_fractional_integer():
    result = diagnostics.decode_response_packet(
        param(struct.pack("<f", 1.5)), parameter_encoding="c_cast"
    )
    assert result["value"] is None and result["interpretation_status"] == "invalid-cast-integer"


def test_real32_keeps_finite_value():
    result = diagnostics.decode_response_packet(
        param(struct.pack("<f", 4500), kind=9), parameter_encoding="bytewise"
    )
    assert result["value"] == 4500


@pytest.mark.parametrize("change", ("crc", "trailing", "short", "signed", "foreign-id"))
def test_wire_fail_closed(change):
    packet = bytearray(param(struct.pack("<i", 4001)))
    if change == "crc":
        packet[10] ^= 1
    elif change == "trailing":
        packet += b"\0"
    elif change == "short":
        packet = packet[:9]
    elif change == "signed":
        packet[2] = 1
    else:
        packet[7] = 21
    with pytest.raises(ValueError):
        diagnostics.decode_response_packet(bytes(packet), parameter_encoding="bytewise")


@pytest.mark.parametrize(
    "kwargs", ({"name": b"BAD\0NONZERO"}, {"count": 0}, {"index": 2}, {"name": b"\xff"})
)
def test_invalid_parameter_envelope(kwargs):
    with pytest.raises((ValueError, UnicodeError)):
        diagnostics.decode_response_packet(param(bytes(4), **kwargs))


class Message:
    def __init__(self, packet, kind, sequence, system=1):
        self.packet, self.kind, self.sequence, self.system = packet, kind, sequence, system

    def get_msgbuf(self):
        return self.packet

    def get_type(self):
        return self.kind

    def get_srcSystem(self):
        return self.system

    def get_srcComponent(self):
        return 1

    def get_seq(self):
        return self.sequence

    def to_dict(self):
        return {"mavpackettype": self.kind, "fixture": "not_runtime_evidence"}


def capture_fixture(tmp_path, messages):
    counter = [10.0]

    def clock():
        counter[0] += 0.01
        return counter[0]

    capture = TeacherTelemetryCaptureV3(
        tmp_path / "capture",
        clock=clock,
        clock_id="CPU-fixture",
        transaction_id="CPU-only",
        local_origin_id="not-EKF",
    )
    for message in messages:
        capture.append_batch([(message, clock())])
    receipt = capture.seal()
    return receipt["receipt"]


def test_closed_actual_writer_source_capability_and_original_nan_union(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "ROOT", tmp_path)
    messages = [
        Message(version(), "AUTOPILOT_VERSION", 1),
        Message(param(struct.pack("<i", -1), sequence=2), "PARAM_VALUE", 2),
    ]
    capture = capture_fixture(tmp_path, messages)
    report = diagnostics.diagnose_parameters(
        capture["path"], capture["sha256"], tmp_path / "result.json"
    )
    assert report["parameter_encoding"] == "bytewise"
    assert report["parameter_rows"][0]["wire"]["value"] == -1
    assert report["parameter_rows"][0]["response_causality_verified"] is False
    assert report["missing_parameter_names"] and not report["errors"]
    assert all(value is False for value in report["claims"].values())
    with pytest.raises(ValueError):
        diagnostics.diagnose_parameters(
            capture["path"], capture["sha256"], tmp_path / "result.json"
        )


def test_changed_version_identity_blocks_encoding(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "ROOT", tmp_path)
    capture = capture_fixture(
        tmp_path,
        [
            Message(version(), "AUTOPILOT_VERSION", 1),
            Message(version(131072, sequence=2), "AUTOPILOT_VERSION", 2),
            Message(param(struct.pack("<i", 4001), sequence=3), "PARAM_VALUE", 3),
        ],
    )
    report = diagnostics.diagnose_parameters(
        capture["path"], capture["sha256"], tmp_path / "result.json"
    )
    assert report["version_identity_consistent"] is False
    assert (
        report["parameter_encoding"] is None
        and report["parameter_rows"][0]["wire"]["value"] is None
    )


def test_original_foreign_parameter_remains_foreign(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "ROOT", tmp_path)
    capture = capture_fixture(
        tmp_path,
        [
            Message(version(), "AUTOPILOT_VERSION", 1),
            Message(
                param(struct.pack("<i", 4001), sequence=2, system=2), "PARAM_VALUE", 2, system=2
            ),
        ],
    )
    report = diagnostics.diagnose_parameters(
        capture["path"], capture["sha256"], tmp_path / "result.json"
    )
    assert not report["parameter_rows"] and report["errors"]
    assert report["actual_parameter_values_observed"] is False


def test_whole_journal_hash_change_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "ROOT", tmp_path)
    capture = capture_fixture(tmp_path, [Message(version(), "AUTOPILOT_VERSION", 1)])
    journal = tmp_path / "capture/raw-packets.journal"
    raw = journal.read_bytes()
    journal.write_bytes(raw[:-1] + bytes((raw[-1] ^ 1,)))
    with pytest.raises(ValueError, match="SHA"):
        diagnostics.diagnose_parameters(
            capture["path"], capture["sha256"], tmp_path / "result.json"
        )
    assert hashlib.sha256(journal.read_bytes()).digest() != hashlib.sha256(raw).digest()


def rewrite_fixture_receipt(binding, mutate):
    """Mutate only synthetic claims; external SHA still cannot excuse inconsistency."""
    path = Path(binding["path"])
    receipt = diagnostics.strict_json(path.read_bytes())
    mutate(receipt)
    raw = diagnostics.canonical(receipt)
    path.write_bytes(raw)
    return {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def test_cannot_omit_conflicting_version_already_in_whole_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "ROOT", tmp_path)
    binding = capture_fixture(
        tmp_path,
        [
            Message(version(16), "AUTOPILOT_VERSION", 1),
            Message(version(131072, sequence=2), "AUTOPILOT_VERSION", 2),
            Message(param(struct.pack("<i", 4001), sequence=3), "PARAM_VALUE", 3),
        ],
    )
    binding = rewrite_fixture_receipt(binding, lambda receipt: receipt["records"].pop(1))
    with pytest.raises(ValueError):
        diagnostics.diagnose_parameters(
            binding["path"], binding["sha256"], tmp_path / "result.json"
        )
    assert not (tmp_path / "result.json").exists()


def test_original_unknown_cannot_be_reclassified_by_receipt_kind(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "ROOT", tmp_path)
    binding = capture_fixture(
        tmp_path,
        [
            Message(version(), "AUTOPILOT_VERSION", 1),
            Message(param(struct.pack("<i", 4001), sequence=2), "UNKNOWN_22", 2),
        ],
    )

    def mutate(receipt):
        receipt["records"][1]["message_type"] = "PARAM_VALUE"
        receipt["packet_attempts"][1]["message_type"] = "PARAM_VALUE"

    binding = rewrite_fixture_receipt(binding, mutate)
    with pytest.raises(ValueError):
        diagnostics.diagnose_parameters(
            binding["path"], binding["sha256"], tmp_path / "result.json"
        )
    assert not (tmp_path / "result.json").exists()


@pytest.mark.parametrize("change", ("packet-sha", "nonbool-source", "nonbool-count"))
def test_primary_receipt_identity_cannot_disagree_with_original_record(
    tmp_path, monkeypatch, change
):
    monkeypatch.setattr(diagnostics, "ROOT", tmp_path)
    binding = capture_fixture(tmp_path, [Message(version(), "AUTOPILOT_VERSION", 1)])

    def mutate(receipt):
        if change == "nonbool-count":
            receipt["record_count"] = True
        else:
            name, value = (
                ("packet_sha256", "0" * 64) if change == "packet-sha" else ("source_accepted", 1)
            )
            receipt["records"][0][name] = value
            receipt["packet_attempts"][0][name] = value

    binding = rewrite_fixture_receipt(binding, mutate)
    with pytest.raises(ValueError):
        diagnostics.diagnose_parameters(
            binding["path"], binding["sha256"], tmp_path / "result.json"
        )
    assert not (tmp_path / "result.json").exists()


def test_projection_embedded_raw_binding_must_equal_selected_receipt_binding(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "ROOT", tmp_path)
    binding = capture_fixture(tmp_path, [Message(version(), "AUTOPILOT_VERSION", 1)])

    def mutate(receipt):
        projected = receipt["records"][0]["projection_record"]
        path = Path(projected["path"])
        raw = path.read_bytes()
        document = diagnostics.strict_json(raw[projected["offset"] :])
        document["raw_record"]["packet_offset"] += 1
        changed = diagnostics.canonical(document)
        rewritten = raw[: projected["offset"]] + changed
        path.write_bytes(rewritten)
        for record in (receipt["records"][0], receipt["packet_attempts"][0]):
            record["projection_record"]["bytes"] = len(changed)
            record["projection_record"]["sha256"] = hashlib.sha256(changed).hexdigest()
        source = receipt["journal_sources"]["projection"]
        source["bytes"] = source["verified_prefix_bytes"] = len(rewritten)
        source["sha256"] = source["verified_prefix_sha256"] = hashlib.sha256(rewritten).hexdigest()

    binding = rewrite_fixture_receipt(binding, mutate)
    with pytest.raises(ValueError):
        diagnostics.diagnose_parameters(
            binding["path"], binding["sha256"], tmp_path / "result.json"
        )
    assert not (tmp_path / "result.json").exists()


def test_raw_header_is_not_opaque_hash_only_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "ROOT", tmp_path)
    binding = capture_fixture(tmp_path, [Message(version(), "AUTOPILOT_VERSION", 1)])

    def mutate(receipt):
        selected = receipt["records"][0]["raw_record"]
        raw_path = Path(selected["path"])
        raw = raw_path.read_bytes()
        offset, size = selected["offset"], selected["bytes"]
        original = raw[offset : offset + size]
        header_size = struct.unpack("<I", original[:4])[0]
        header = diagnostics.strict_json(original[4 : 4 + header_size])
        header["record_index"] = 9
        changed_header = diagnostics.canonical(header)
        assert len(changed_header) == header_size
        changed = (
            struct.pack("<I", len(changed_header)) + changed_header + original[4 + header_size :]
        )
        rewritten = raw[:offset] + changed + raw[offset + size :]
        raw_path.write_bytes(rewritten)
        new_hash = hashlib.sha256(changed).hexdigest()
        for record in (receipt["records"][0], receipt["packet_attempts"][0]):
            record["raw_record"]["sha256"] = new_hash
        raw_source = receipt["journal_sources"]["raw"]
        raw_source["sha256"] = raw_source["verified_prefix_sha256"] = hashlib.sha256(
            rewritten
        ).hexdigest()

        projected = receipt["records"][0]["projection_record"]
        projection_path = Path(projected["path"])
        projection_raw = projection_path.read_bytes()
        document = diagnostics.strict_json(projection_raw[projected["offset"] :])
        document["raw_record"]["sha256"] = new_hash
        changed_projection = diagnostics.canonical(document)
        assert len(changed_projection) == projected["bytes"]
        rewritten_projection = projection_raw[: projected["offset"]] + changed_projection
        projection_path.write_bytes(rewritten_projection)
        for record in (receipt["records"][0], receipt["packet_attempts"][0]):
            record["projection_record"]["sha256"] = hashlib.sha256(changed_projection).hexdigest()
        source = receipt["journal_sources"]["projection"]
        source["sha256"] = source["verified_prefix_sha256"] = hashlib.sha256(
            rewritten_projection
        ).hexdigest()

    binding = rewrite_fixture_receipt(binding, mutate)
    with pytest.raises(ValueError):
        diagnostics.diagnose_parameters(
            binding["path"], binding["sha256"], tmp_path / "result.json"
        )
    assert not (tmp_path / "result.json").exists()


def test_repeated_same_firmware_capabilities_ignore_only_packet_sequence(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "ROOT", tmp_path)
    binding = capture_fixture(
        tmp_path,
        [
            Message(version(sequence=1), "AUTOPILOT_VERSION", 1),
            Message(version(sequence=2), "AUTOPILOT_VERSION", 2),
            Message(param(struct.pack("<i", -1), sequence=3), "PARAM_VALUE", 3),
        ],
    )
    report = diagnostics.diagnose_parameters(
        binding["path"], binding["sha256"], tmp_path / "result.json"
    )
    assert report["version_identity_consistent"] is True
    assert report["parameter_encoding"] == "bytewise"
    assert report["parameter_rows"][0]["wire"]["value"] == -1


def test_conflicting_version_cannot_hide_as_nonselected_receipt_kind(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "ROOT", tmp_path)
    binding = capture_fixture(
        tmp_path,
        [
            Message(version(16), "AUTOPILOT_VERSION", 1),
            Message(version(131072, sequence=2), "AUTOPILOT_VERSION", 2),
            Message(param(struct.pack("<i", 4001), sequence=3), "PARAM_VALUE", 3),
        ],
    )

    def mutate(receipt):
        # Keep every original byte/hash/index/coverage claim, change only kind.
        receipt["records"][1]["message_type"] = "HEARTBEAT"
        receipt["packet_attempts"][1]["message_type"] = "HEARTBEAT"

    binding = rewrite_fixture_receipt(binding, mutate)
    with pytest.raises(ValueError):
        diagnostics.diagnose_parameters(
            binding["path"], binding["sha256"], tmp_path / "result.json"
        )
    assert not (tmp_path / "result.json").exists()
