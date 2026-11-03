"""Cross-platform mutual exclusion for PX4/Gazebo SITL runs.

The lock is deliberately a small directory so acquisition is atomic across
shells, Python workers, and different worktrees.  The owner record includes
the PID and process creation time; a dead owner is reclaimed only after the
same two-minute grace period used by the legacy shell lock.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from scripts.model_round_guard import _process_snapshot

try:
    import psutil  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]


CONTAINER_NAME_RE = re.compile(r"^(?:md-|r15-worker)")
SITL_LOCK_HELD_ENV = "MULTIDETECT_SITL_LOCK_HELD"
SITL_SLOT_ENV = "MULTIDETECT_SITL_SLOT"


def configured_slot_count() -> int:
    try:
        count = int(os.environ.get("MULTIDETECT_SITL_SLOTS", "1"))
    except ValueError as exc:
        raise ValueError("MULTIDETECT_SITL_SLOTS must be a positive integer") from exc
    if count < 1:
        raise ValueError("MULTIDETECT_SITL_SLOTS must be a positive integer")
    return count


def default_lock_path() -> Path:
    configured = os.environ.get("MULTIDETECT_SITL_LOCK")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        return Path(r"D:\Projects\mdwt\_scratch\sitl.lock")
    return Path("/data/scratch/multidetect-sitl.lock")


def available_memory_gb() -> float:
    if psutil is not None:
        return float(psutil.virtual_memory().available) / 1_000_000_000
    if os.name == "nt":
        try:
            output = subprocess.check_output(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "[int](Get-Counter '\\Memory\\Available MBytes').CounterSamples[0].CookedValue",
                ],
                text=True,
            )
            return float(output.strip()) / 1024
        except (OSError, subprocess.SubprocessError, ValueError):
            return 0.0
    values = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, value = line.split(":", 1)
            values[key] = int(value.strip().split()[0])
    except (OSError, ValueError):
        return 0.0
    # MemAvailable is reported in KiB on Linux.
    return values.get("MemAvailable", 0) / 1_000_000


def running_sitl_containers(
    runner: Callable[..., object] = subprocess.run,
    docker: str = "docker",
) -> tuple[str, ...] | None:
    try:
        result = runner(
            [docker, "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(result, "returncode", 0) != 0:
        return None
    names = str(getattr(result, "stdout", "") or "").splitlines()
    return tuple(name for name in names if CONTAINER_NAME_RE.match(name.strip()))


def _parse_owner(text: str) -> tuple[int, float | None] | None:
    fields = text.split()
    if len(fields) < 2:
        return None
    try:
        pid = int(fields[1])
    except ValueError:
        return None
    start = None
    for field in fields[2:]:
        if field.startswith("start="):
            try:
                start = float(field.split("=", 1)[1])
            except ValueError:
                pass
    return pid, start


class SitlLock:
    def __init__(
        self,
        path: Path | None = None,
        label: str = "nightly-regression",
        retry_s: float = 60.0,
        stale_s: float = 120.0,
        min_memory_gb: float = 5.0,
        runner: Callable[..., object] = subprocess.run,
        memory_fn: Callable[[], float] = available_memory_gb,
        slots: int | None = None,
        slot: int | None = None,
    ) -> None:
        self.root_path = Path(path or default_lock_path())
        self.slots = configured_slot_count() if slots is None else int(slots)
        if self.slots < 1:
            raise ValueError("slots must be a positive integer")
        self.slot = slot
        self.path = self.root_path
        self.label, self.retry_s, self.stale_s = label, retry_s, stale_s
        self.min_memory_gb = min_memory_gb
        self.runner, self.memory_fn = runner, memory_fn
        self.owner = self.path / "owner"
        self.acquired = False
        self.owner_text = ""

    def _reclaim_stale(self) -> None:
        if not self.owner.exists():
            return
        try:
            age = time.time() - self.owner.stat().st_mtime
            parsed = _parse_owner(self.owner.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            return
        if age <= self.stale_s or parsed is None:
            return
        pid, recorded_start = parsed
        alive, observed_start = _process_snapshot(pid)
        same_process = alive
        if recorded_start is not None and observed_start is not None:
            same_process = abs(recorded_start - observed_start) <= 1e-3
        if not same_process:
            stale_path = self.path.with_name(f"{self.path.name}.stale-{uuid.uuid4().hex}")
            try:
                self.path.rename(stale_path)
            except FileNotFoundError:
                return
            except OSError:
                return
            shutil.rmtree(stale_path, ignore_errors=True)

    def _ready(self) -> bool:
        containers = running_sitl_containers(self.runner)
        if self.slots > 1 and self.slot is not None and containers is not None:
            slot_marker = re.compile(r"(?:-slot-|^r15-worker-)(\d+)(?:-|$)")
            active_slots = {
                int(path.name.rsplit(".slot-", 1)[1])
                for path in self.root_path.parent.glob(f"{self.root_path.name}.slot-*")
                if path.is_dir() and path.name.rsplit(".slot-", 1)[1].isdigit()
            }
            foreign = []
            for name in containers:
                match = slot_marker.search(name)
                if match is None or int(match.group(1)) == self.slot:
                    foreign.append(name)
            containers = tuple(foreign)
            memory_required = self.min_memory_gb
        else:
            memory_required = self.min_memory_gb
        return containers == () and self.memory_fn() >= memory_required

    def acquire(self, wait: bool = True) -> None:
        while True:
            self.root_path.parent.mkdir(parents=True, exist_ok=True)
            if self.slots > 1 and self.root_path.exists():
                if not wait:
                    raise RuntimeError(f"legacy SITL lock busy: {self.root_path}")
                time.sleep(self.retry_s)
                continue
            candidates = (self.slot,) if self.slot is not None else range(self.slots)
            acquired_path = None
            for slot in candidates:
                if slot is None or slot < 0 or slot >= self.slots:
                    raise ValueError(f"SITL slot outside configured range: {slot}")
                self.path = self.root_path if self.slots == 1 else self.root_path.with_name(
                    f"{self.root_path.name}.slot-{slot}"
                )
                self.owner = self.path / "owner"
                self._reclaim_stale()
                try:
                    self.path.mkdir()
                    self.slot = slot
                    self.owner = self.path / "owner"
                    acquired_path = self.path
                    break
                except FileExistsError:
                    continue
            if acquired_path is None:
                if not wait:
                    raise RuntimeError(f"SITL slots busy: {self.root_path}")
                time.sleep(self.retry_s)
                continue
            alive, start = _process_snapshot(os.getpid())
            start = start if alive and start is not None else time.time()
            self.owner_text = (
                f"{self.label} {os.getpid()} {datetime.now(timezone.utc).isoformat()} "
                f"start={start}\n"
            )
            try:
                self.owner.write_text(self.owner_text, encoding="utf-8")
            except BaseException:
                shutil.rmtree(self.path, ignore_errors=True)
                raise
            self.acquired = True
            while not self._ready():
                if not wait:
                    self.release()
                    raise RuntimeError("SITL prerequisites are busy or memory is below threshold")
                time.sleep(self.retry_s)
            return

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            if self.owner.read_text(encoding="utf-8") == self.owner_text:
                self.owner.unlink(missing_ok=True)
                self.path.rmdir()
        except OSError:
            pass
        self.acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


@contextmanager
def sitl_lock_context(lock: object, *acquire_args: object, **acquire_kwargs: object):
    """Acquire a SITL lock unless the current process already holds its parent lock.

    The helper accepts both the shared :class:`SitlLock` API and the older
    script-local lock APIs, which all expose ``acquire`` and ``release``.
    Children inherit the marker while the body runs; the previous value is
    restored when leaving the context.
    """
    previous = os.environ.get(SITL_LOCK_HELD_ENV)
    previous_slot = os.environ.get(SITL_SLOT_ENV)
    acquired = previous != "1"
    used_context_manager = False
    if acquired:
        acquire = getattr(lock, "acquire", None)
        if acquire is not None:
            acquire(*acquire_args, **acquire_kwargs)
        else:
            lock.__enter__()  # type: ignore[attr-defined]
            used_context_manager = True
    slot = getattr(lock, "slot", None)
    if acquired and slot is not None:
        os.environ[SITL_SLOT_ENV] = str(slot)
    os.environ[SITL_LOCK_HELD_ENV] = "1"
    try:
        yield lock
    finally:
        if previous is None:
            os.environ.pop(SITL_LOCK_HELD_ENV, None)
        else:
            os.environ[SITL_LOCK_HELD_ENV] = previous
        if previous_slot is None:
            os.environ.pop(SITL_SLOT_ENV, None)
        else:
            os.environ[SITL_SLOT_ENV] = previous_slot
        if acquired:
            if used_context_manager:
                lock.__exit__(None, None, None)  # type: ignore[attr-defined]
            else:
                lock.release()  # type: ignore[attr-defined]
