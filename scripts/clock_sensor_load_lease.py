"""Run the clock/sensor load suite while holding a shared SITL slot lease."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
SITL_SLOTS = 3

sys.path.insert(0, str(ROOT))
from scripts.sitl_lock import SitlLock, default_lock_path  # noqa: E402


def run_with_slot_lease(
    script: str | Path,
    arguments: list[str] | tuple[str, ...] = (),
    *,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
    lock_factory: Callable[..., SitlLock] | None = None,
) -> int:
    """Acquire any free slot, then run the shell suite with the lease marker."""
    runner = runner or subprocess.run
    lock_factory = lock_factory or SitlLock
    script_path = Path(script)
    if not script_path.is_absolute():
        script_path = (ROOT / script_path).resolve()

    lock = lock_factory(
        default_lock_path(),
        label="clock-sensor-load",
        slots=SITL_SLOTS,
    )
    lock.acquire(wait=True)
    try:
        if lock.slot is None:
            raise RuntimeError("SITL lease acquired without selecting a slot")
        env = os.environ.copy()
        env["MULTIDETECT_SITL_SLOTS"] = str(SITL_SLOTS)
        env["MULTIDETECT_SITL_SLOT"] = str(lock.slot)
        env["MULTIDETECT_SITL_LOCK_HELD"] = "1"
        result = runner(
            ["bash", str(script_path), *arguments],
            cwd=ROOT,
            env=env,
            check=False,
        )
        return int(result.returncode)
    finally:
        lock.release()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: clock_sensor_load_lease.py SCRIPT [ARGS...]", file=sys.stderr)
        return 2
    return run_with_slot_lease(argv[0], argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
