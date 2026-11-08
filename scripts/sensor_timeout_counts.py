"""Shared PX4 STATUSTEXT sensor timeout parsing helpers."""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

SENSOR_KEYS = ("accel", "gyro", "mag")
_SENSOR_RE = re.compile(
    r"\b(accel(?:erometer)?|gyro(?:scope)?|mag(?:netometer)?)\b", re.IGNORECASE
)
_TIMEOUT_RE = re.compile(r"\bTIMEOUT\b", re.IGNORECASE)


def sensor_timeout_kinds(text: str) -> tuple[str, ...]:
    """Return canonical sensor keys mentioned in a timeout message."""
    if not _TIMEOUT_RE.search(text):
        return ()
    found = []
    for match in _SENSOR_RE.finditer(text):
        token = match.group(1).lower()
        key = (
            "accel"
            if token.startswith("accel")
            else "gyro"
            if token.startswith("gyro")
            else "mag"
        )
        if key not in found:
            found.append(key)
    return tuple(found)


def _is_statustext(row: dict[str, Any]) -> bool:
    kind = str(row.get("message_type", row.get("mavpackettype", ""))).upper()
    return kind == "STATUSTEXT"


def sensor_timeout_kinds_from_row(row: dict[str, Any]) -> tuple[str, ...]:
    if not _is_statustext(row):
        return ()
    return sensor_timeout_kinds(str(row.get("text", "")))


def count_sensor_timeouts(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts = {key: 0 for key in SENSOR_KEYS}
    for row in rows:
        for key in sensor_timeout_kinds_from_row(row):
            counts[key] += 1
    return counts


def iter_jsonl_rows(path: Path) -> Iterator[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else Path.open
    try:
        with opener(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    yield row
    except (OSError, EOFError):
        return


def count_sensor_timeouts_in_tree(root: Path) -> dict[str, int]:
    """Count MAVLink STATUSTEXT and legacy plain-text timeout lines below root."""
    counts = {key: 0 for key in SENSOR_KEYS}
    mavlink_paths = sorted(
        path
        for path in root.rglob("mavlink.jsonl*")
        if path.is_file() and path.name in {"mavlink.jsonl", "mavlink.jsonl.gz"}
    )
    if mavlink_paths:
        paths = mavlink_paths
    else:
        paths = sorted(path for path in root.rglob("*") if path.is_file())
    for path in paths:
        if path.name in {"mavlink.jsonl", "mavlink.jsonl.gz"}:
            source = count_sensor_timeouts(iter_jsonl_rows(path))
        elif not mavlink_paths and path.suffix in {".log", ".txt"}:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            source = {key: 0 for key in SENSOR_KEYS}
            for line in text.splitlines():
                for key in sensor_timeout_kinds(line):
                    source[key] += 1
        else:
            continue
        for key in SENSOR_KEYS:
            counts[key] += source[key]
    return counts
