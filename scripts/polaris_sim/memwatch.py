#!/usr/bin/env python3
"""Stop only leased Polaris SITL containers before they reach their memory cap."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

_MEMORY_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt](?:i?b)?|b)?\s*$", re.IGNORECASE)
_OWNED_CONTAINER_RE = re.compile(r"^md-polaris-sim-slot-(5|6|7)$")
_UNIT_BY_SLOT = {5: "psim-sitl-slot-5", 6: "psim-sitl-slot-6", 7: "psim-sitl-slot-7"}
_STOP = threading.Event()


def parse_memory_bytes(value: str) -> int:
    """Parse Docker-style memory quantities such as 16g or 512MiB."""
    match = _MEMORY_RE.fullmatch(value)
    if not match:
        raise ValueError(f"invalid memory size: {value!r}")
    amount = float(match.group(1))
    unit = (match.group(2) or "b").lower()
    multipliers = {
        "b": 1,
        "k": 1024,
        "m": 1024**2,
        "g": 1024**3,
        "t": 1024**4,
        "kb": 1000,
        "kib": 1024,
        "mb": 1000**2,
        "mib": 1024**2,
        "gb": 1000**3,
        "gib": 1024**3,
        "tb": 1000**4,
        "tib": 1024**4,
    }
    if unit not in multipliers:
        raise ValueError(f"invalid memory unit: {unit}")
    return int(amount * multipliers[unit])


def parse_docker_usage(value: str) -> int:
    """Extract the current usage from docker stats' "used / limit" output."""
    used = value.split("/", 1)[0].strip()
    return parse_memory_bytes(used)


def _write_state(path: Path, update: dict[str, object]) -> None:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {}
    if not isinstance(state, dict):
        raise ValueError(f"invalid state file: {path}")
    state.update(update)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _docker_usage(container: str) -> int | None:
    try:
        result = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", container],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    try:
        return parse_docker_usage(result.stdout.strip())
    except ValueError:
        return None


def _stop_owned_container(container: str) -> None:
    match = _OWNED_CONTAINER_RE.fullmatch(container)
    if not match:
        raise ValueError(f"refusing to stop an unowned container: {container}")
    slot = int(match.group(1))
    # Stop the transient unit first so its Restart=on-failure policy cannot recreate the container.
    result = subprocess.run(
        ["systemctl", "stop", _UNIT_BY_SLOT[slot]],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"systemd stop failed for slot {slot}: {detail}")
    result = subprocess.run(
        ["docker", "stop", "--time", "5", container],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        if "no such container" not in detail.lower() and "is not running" not in detail.lower():
            raise RuntimeError(f"docker stop failed for {container}: {detail}")


def check_once(
    containers: tuple[str, ...],
    memory_limit_bytes: int,
    state_file: Path,
    *,
    usage_reader: Callable[[str], int | None] = _docker_usage,
    stopper: Callable[[str], None] = _stop_owned_container,
    now: Callable[[], float] = time.time,
) -> list[dict[str, object]]:
    threshold = int(memory_limit_bytes * 0.9)
    events = []
    for container in containers:
        if not _OWNED_CONTAINER_RE.fullmatch(container):
            raise ValueError(f"refusing to monitor an unowned container: {container}")
        usage = usage_reader(container)
        if usage is None or usage <= threshold:
            continue
        stop_error = None
        try:
            stopper(container)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            stop_error = str(exc)
        reason = (
            f"容器 {container} 内存达到 {usage} bytes，超过配置上限的 90% "
            f"（上限 {memory_limit_bytes} bytes）；"
            + (f"停止失败：{stop_error}" if stop_error else "已停止该仿真容器。")
        )
        event = {
            "container": container,
            "usage_bytes": usage,
            "limit_bytes": memory_limit_bytes,
            "reason": reason,
            "at": now(),
        }
        if stop_error:
            event["stop_error"] = stop_error
        _write_state(
            state_file,
            {
                "memory_watchdog_container": container,
                "memory_watchdog_reason": reason,
                "memory_watchdog_at": event["at"],
            },
        )
        events.append(event)
    return events


def _set_pid(state_file: Path, pid: int | None) -> None:
    _write_state(state_file, {"memwatch_pid": pid})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--memory-limit", required=True)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--container", action="append", required=True)
    args = parser.parse_args(argv)
    try:
        if args.interval <= 0:
            raise ValueError("--interval must be positive")
        memory_limit = parse_memory_bytes(args.memory_limit)
        if memory_limit <= 0:
            raise ValueError("memory limit must be positive")
        for container in args.container:
            if not _OWNED_CONTAINER_RE.fullmatch(container):
                raise ValueError(f"refusing to monitor an unowned container: {container}")
        _set_pid(args.state_file, os.getpid())
        signal.signal(signal.SIGTERM, lambda *_: _STOP.set())
        signal.signal(signal.SIGINT, lambda *_: _STOP.set())
        while not _STOP.is_set():
            check_once(tuple(args.container), memory_limit, args.state_file)
            _STOP.wait(args.interval)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"polaris-sim-memwatch: {exc}", file=sys.stderr)
        return 2
    finally:
        try:
            _set_pid(args.state_file, None)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
