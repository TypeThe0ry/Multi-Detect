from __future__ import annotations

import struct

from scripts.analyze_px4_ulog import _parse_internal, render_markdown


def _message(kind: str, payload: bytes) -> bytes:
    return struct.pack("<HB", len(payload), ord(kind)) + payload


def _synthetic_ulog() -> bytes:
    data = bytearray(struct.pack("<8sQ", b"ULog\x01\x12\x35", 0))
    data.extend(_message("F", b"vehicle_status:bool failsafe;uint8_t nav_state"))
    data.extend(_message("F", b"cpuload:uint64_t timestamp;float load"))
    data.extend(
        _message("A", struct.pack("<BH", 0, 1) + b"vehicle_status:bool failsafe;uint8_t nav_state")
    )
    data.extend(_message("A", struct.pack("<BH", 0, 2) + b"cpuload:uint64_t timestamp;float load"))
    data.extend(_message("D", struct.pack("<H?B", 1, True, 4)))
    data.extend(_message("D", struct.pack("<HQf", 2, 1_000_000, 0.25)))
    data.extend(_message("D", struct.pack("<HQf", 2, 4_000_000, 0.75)))
    data.extend(_message("L", struct.pack("<QB", 3_500_000, 2) + b"lockstep stalled\0"))
    return bytes(data)


def test_minimal_parser_reads_topics_gaps_failsafe_and_console():
    parsed = _parse_internal(_synthetic_ulog())
    report = render_markdown(parsed, gap_seconds=1.0)

    assert len(parsed.topics["cpuload"].rows) == 2
    assert "3.000000" in report
    assert "failsafe=1" in report
    assert "load=0.75" in report
    assert "lockstep stalled" in report
    assert "Parser: `minimal`" in report
