#!/usr/bin/env python3
"""Summarize PX4 ULog stalls as Markdown.

The script accepts a ``.ulg`` file or an episode directory.  ``pyulog`` is
used when installed; the small reader below covers the ULog header, format,
data, and logging messages needed for offline stall investigation.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import re
import struct
from pathlib import Path
from typing import Any

MAGIC = b"ULog\x01\x12\x35"
HEADER_SIZE = 16


@dataclasses.dataclass
class Topic:
    name: str
    fields: list[tuple[str, str]]
    rows: list[dict[str, Any]] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class ConsoleMessage:
    timestamp_us: int
    level: int
    text: str


@dataclasses.dataclass
class ParsedLog:
    path: Path | None
    topics: dict[str, Topic]
    console: list[ConsoleMessage]
    warnings: list[str]
    used_pyulog: bool = False

    @property
    def last_timestamp_us(self) -> int | None:
        values = [
            int(row[field])
            for topic in self.topics.values()
            for row in topic.rows
            for field in ("timestamp", "timestamp_sample", "time")
            if field in row and isinstance(row[field], (int, float))
        ]
        return max(values, default=None)


def _field_parts(spec: str) -> tuple[str, str]:
    spec = spec.strip()
    if not spec:
        return "", ""
    type_name, _, name = spec.strip().partition(" ")
    return type_name.strip(), name.strip()


def _type_info(type_name: str) -> tuple[str, int, str]:
    aliases = {
        "int8_t": "b",
        "uint8_t": "B",
        "int16_t": "h",
        "uint16_t": "H",
        "int32_t": "i",
        "uint32_t": "I",
        "int64_t": "q",
        "uint64_t": "Q",
        "float": "f",
        "double": "d",
        "char": "c",
        "bool": "B",
    }
    match = re.fullmatch(r"([^\[]+)(?:\[(\d+)\])?", type_name)
    if not match:
        return "B", 1, "<B"
    code, count = aliases.get(match.group(1), match.group(1)), int(match.group(2) or 1)
    if code not in "bBhHiIqQfdc":
        return "B", count, "<B"
    formats = {
        "b": "<b",
        "B": "<B",
        "h": "<h",
        "H": "<H",
        "i": "<i",
        "I": "<I",
        "q": "<q",
        "Q": "<Q",
        "f": "<f",
        "d": "<d",
        "c": "<c",
    }
    return code, count, formats[code]


def _decode_row(payload: bytes, fields: list[tuple[str, str]]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    offset = 0
    for type_name, name in fields:
        code, count, fmt = _type_info(type_name)
        size = struct.calcsize(fmt) * count
        chunk = payload[offset : offset + size]
        offset += size
        if len(chunk) != size:
            break
        values = [struct.unpack_from(fmt, chunk, i * struct.calcsize(fmt))[0] for i in range(count)]
        if code == "c":
            value: Any = b"".join(values).split(b"\0", 1)[0].decode("utf-8", "replace")
        else:
            value = values[0] if count == 1 else values
        row[name] = value
    return row


def _parse_internal(raw: bytes, path: Path | None = None) -> ParsedLog:
    warnings: list[str] = []
    if len(raw) < HEADER_SIZE or raw[:7] != MAGIC:
        raise ValueError("not a ULog file (bad magic or truncated header)")
    topics: dict[str, Topic] = {}
    format_defs: dict[str, list[tuple[str, str]]] = {}
    ids: dict[int, str] = {}
    console: list[ConsoleMessage] = []
    offset = HEADER_SIZE
    while offset + 3 <= len(raw):
        size = struct.unpack_from("<H", raw, offset)[0]
        kind = chr(raw[offset + 2])
        payload_start = offset + 3
        payload_end = payload_start + size
        if payload_end > len(raw):
            warnings.append(f"truncated message {kind!r} at offset {offset}")
            break
        payload = raw[payload_start:payload_end]
        offset = payload_end
        try:
            if kind == "F":
                text = payload.decode("utf-8", "replace")
                type_name, _, field_text = text.partition(":")
                fields = [_field_parts(item) for item in field_text.split(";")]
                fields = [(t, n) for t, n in fields if t and n]
                format_defs[type_name.strip()] = fields
                topics.setdefault(type_name.strip(), Topic(type_name.strip(), fields))
            elif kind == "A":
                if len(payload) < 3:
                    continue
                multi_id, msg_id = struct.unpack_from("<BH", payload)
                text = payload[3:].decode("utf-8", "replace")
                # ULog ``A`` records normally contain only the topic name; its
                # fields were declared by a preceding ``F`` record.  A few
                # synthetic/legacy fixtures include the fields inline, so keep
                # accepting that form as well.
                name, separator, field_text = text.partition(":")
                name = name.strip()
                if separator:
                    fields = [_field_parts(item) for item in field_text.split(";")]
                    fields = [(t, n) for t, n in fields if t and n]
                    format_defs[name] = fields
                else:
                    fields = format_defs.get(name, topics.get(name, Topic(name, [])).fields)
                topics.setdefault(name, Topic(name, fields))
                ids[msg_id] = name
            elif kind == "D":
                if len(payload) < 2:
                    continue
                msg_id = struct.unpack_from("<H", payload)[0]
                name = ids.get(msg_id)
                if name in topics:
                    topics[name].rows.append(_decode_row(payload[2:], topics[name].fields))
            elif kind in {"L", "C"}:
                header_size = 11 if kind == "C" else 9
                if len(payload) >= header_size:
                    if kind == "C":
                        level, tag, timestamp = struct.unpack_from("<BHQ", payload)
                    else:
                        level, timestamp = struct.unpack_from("<BQ", payload)
                    text = payload[header_size:].split(b"\0", 1)[0].decode("utf-8", "replace")
                    if kind == "C":
                        text = f"[{tag}] {text}"
                    console.append(ConsoleMessage(timestamp, level, text))
            elif kind == "O":
                duration_ms = struct.unpack_from("<H", payload)[0]
                warnings.append(f"dropout message at {offset - size - 3}: {duration_ms} ms")
        except (struct.error, UnicodeError, ValueError) as exc:
            warnings.append(f"could not decode {kind!r} at offset {offset}: {exc}")
    return ParsedLog(path, topics, console, warnings)


def parse_ulog(path: str | Path) -> ParsedLog:
    path = Path(path)
    try:
        pyulog = importlib.import_module("pyulog")
    except ImportError:
        pyulog = None
    if pyulog is not None:
        try:
            obj = pyulog.ULog(str(path))
            topics: dict[str, Topic] = {}
            for item in getattr(obj, "data_list", []):
                fields = [("B", name) for name in item.data.keys()]
                rows = []
                length = len(next(iter(item.data.values()), []))
                for i in range(length):
                    rows.append(
                        {
                            name: values[i].item() if hasattr(values[i], "item") else values[i]
                            for name, values in item.data.items()
                        }
                    )
                topics[item.name] = Topic(item.name, fields, rows)
            console = [
                ConsoleMessage(int(row.timestamp), int(row.log_level), row.message)
                for row in getattr(obj, "logged_messages", [])
            ]
            return ParsedLog(path, topics, console, [], used_pyulog=True)
        except Exception:
            # A partially supported pyulog release should not make the tool unusable.
            pass
    return _parse_internal(path.read_bytes(), path)


def discover_logs(source: str | Path) -> list[Path]:
    path = Path(source)
    if path.is_file():
        return [path]
    return sorted(path.rglob("*.ulg"))


def _timestamp_field(topic: Topic) -> str | None:
    for field in ("timestamp", "timestamp_sample", "time"):
        if any(field in row for row in topic.rows):
            return field
    return None


def _keyword_topics(parsed: ParsedLog, keywords: tuple[str, ...]) -> list[str]:
    return sorted(name for name in parsed.topics if any(word in name.lower() for word in keywords))


def _fmt_time(value: int | float | None) -> str:
    return "-" if value is None else f"{float(value) / 1_000_000:.6f}"


def render_markdown(parsed: ParsedLog, gap_seconds: float = 1.0) -> str:
    lines = [
        f"# PX4 ULog stall analysis: `{parsed.path or '<bytes>'}`",
        "",
        f"Parser: `{'pyulog' if parsed.used_pyulog else 'minimal'}`",
        "",
    ]
    lines += [
        "## Last sample per topic",
        "",
        "| Topic | Samples | Timestamp field | Last timestamp (s) | Max gap (s) |",
        "| --- | ---: | --- | ---: | ---: |",
    ]
    gap_rows: list[tuple[str, float, float, float]] = []
    for name in sorted(parsed.topics):
        topic = parsed.topics[name]
        field = _timestamp_field(topic)
        times = sorted(
            float(row[field])
            for row in topic.rows
            if field and isinstance(row.get(field), (int, float))
        )
        gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
        max_gap = max(gaps, default=0.0) / 1_000_000
        if max_gap >= gap_seconds:
            gap_rows.append(
                (name, max_gap, times[gaps.index(max(gaps))] / 1_000_000, max(gaps) / 1_000_000)
            )
        last_time = _fmt_time(times[-1] if times else None)
        lines.append(
            f"| `{name}` | {len(topic.rows)} | `{field or '-'}` | "
            f"{last_time} | {max_gap:.6f} |"
        )
    lines += ["", "## Gaps above threshold", ""]
    if gap_rows:
        lines += ["| Topic | Gap (s) | Gap starts (s) |", "| --- | ---: | ---: |"]
        for name, gap, start, _ in sorted(gap_rows, key=lambda item: (-item[1], item[0])):
            lines.append(f"| `{name}` | {gap:.6f} | {start:.6f} |")
    else:
        lines.append(f"No topic gap >= {gap_seconds:.3f} s was found.")

    lines += ["", "## CPU and task load", ""]
    cpu_names = _keyword_topics(parsed, ("cpuload", "task_stack", "task_runtime", "load"))
    if cpu_names:
        for name in cpu_names:
            topic = parsed.topics[name]
            sample = topic.rows[-1] if topic.rows else {}
            lines.append(
                f"- `{name}`: last sample {_fmt_time(sample.get('timestamp'))}; "
                + ", ".join(
                    f"`{key}={value}`" for key, value in sample.items() if key != "timestamp"
                )
            )
    else:
        lines.append("No cpuload/task CPU topic was logged.")

    lines += ["", "## Estimator/sensor timeouts", ""]
    timeout_names = _keyword_topics(
        parsed, ("timeout", "estimator_status", "sensor_status", "sensor_combined")
    )
    timeout_lines = []
    for name in timeout_names:
        for row in parsed.topics[name].rows:
            text = " ".join(
                f"{k}={v}"
                for k, v in row.items()
                if any(token in k.lower() for token in ("timeout", "fault", "error", "flags"))
            )
            if text:
                timeout_lines.append(f"- `{name}`: {text}")
    timeout_msgs = [
        msg
        for msg in parsed.console
        if any(word in msg.text.lower() for word in ("timeout", "timed out", "stale", "no data"))
    ]
    timeout_lines.extend(
        f"- `{_fmt_time(msg.timestamp_us)}s` [{msg.level}] {msg.text}" for msg in timeout_msgs
    )
    lines.extend(
        timeout_lines[-30:] or ["No non-zero timeout/fault fields were found in matching topics."]
    )

    lines += ["", "## Failsafe flags", ""]
    failsafe = _keyword_topics(parsed, ("failsafe", "vehicle_status", "commander_state"))
    for name in failsafe:
        for row in parsed.topics[name].rows[-5:]:
            fields = [
                (k, v)
                for k, v in row.items()
                if "failsafe" in k.lower() or k in {"nav_state", "arming_state"}
            ]
            if fields:
                lines.append(f"- `{name}`: " + ", ".join(f"`{k}={v}`" for k, v in fields))
    if not failsafe:
        lines.append("No failsafe-related topic was logged.")

    lines += ["", "## Lockstep-related topics and messages", ""]
    lockstep_names = _keyword_topics(
        parsed, ("lockstep", "simulator", "time_sync", "timesync", "scheduler")
    )
    lines.extend(
        f"- topic `{name}` ({len(parsed.topics[name].rows)} samples)" for name in lockstep_names
    )
    lock_msgs = [
        msg
        for msg in parsed.console
        if "lockstep" in msg.text.lower()
        or "simulator" in msg.text.lower()
        or "time sync" in msg.text.lower()
    ]
    lines.extend(
        f"- `{_fmt_time(msg.timestamp_us)}s` [{msg.level}] {msg.text}" for msg in lock_msgs
    )
    if not lockstep_names and not lock_msgs:
        lines.append("No lockstep-related topic or console message was found.")

    lines += ["", "## Console messages around the last vehicle sample", ""]
    freeze = parsed.last_timestamp_us
    nearby = [
        msg
        for msg in parsed.console
        if freeze is None or abs(msg.timestamp_us - freeze) <= 10_000_000
    ]
    lines.extend(f"- `{_fmt_time(msg.timestamp_us)}s` [{msg.level}] {msg.text}" for msg in nearby)
    if not nearby:
        lines.append("No console messages within +/-10 s of the last topic timestamp.")
    if parsed.warnings:
        lines += ["", "## Parser warnings", ""]
        lines.extend(f"- {warning}" for warning in parsed.warnings)
    return "\n".join(lines) + "\n"


def analyze(source: str | Path, gap_seconds: float = 1.0) -> str:
    logs = discover_logs(source)
    if not logs:
        raise FileNotFoundError(f"no .ulg files found under {source}")
    sections = [render_markdown(parse_ulog(path), gap_seconds) for path in logs]
    return "\n---\n\n".join(sections)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="episode directory or .ulg file")
    parser.add_argument(
        "--gap-seconds", type=float, default=1.0, help="report topic gaps at or above this value"
    )
    parser.add_argument(
        "-o", "--output", type=Path, help="write Markdown to this file instead of stdout"
    )
    args = parser.parse_args(argv)
    report = analyze(args.source, args.gap_seconds)
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    else:
        print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
