"""Cross-platform mutual exclusion for PX4/Gazebo SITL runs.

The lock is deliberately a small directory so acquisition is atomic across
shells, Python workers, and different worktrees.  The owner record includes
the PID and process creation time; a dead owner is reclaimed only after the
same two-minute grace period used by the legacy shell lock.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
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
SITL_PRIORITY_ENV = "MULTIDETECT_SITL_PRIORITY"


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


def _ticket_candidates(ticket: Path) -> range | tuple[int, ...] | None:
    """Unknown/old tickets conservatively compete for every candidate slot."""
    try:
        lines = ticket.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ()  # The earlier waiter has acquired or withdrawn its ticket.
    except (OSError, UnicodeError):
        return None
    try:
        scope = json.loads(lines[1])
    except (IndexError, ValueError):
        return None
    if not isinstance(scope, dict) or "sitl_slot" not in scope:
        return None
    count, slot = scope.get("sitl_slots"), scope["sitl_slot"]
    if type(count) is not int or count < 1:
        return None
    # One-slot clients use the unsuffixed legacy/global lock directory.
    if count == 1:
        return None
    if slot is None:
        return range(count)
    if type(slot) is not int or not 0 <= slot < count:
        return None
    return (slot,)


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
        priority: int | None = None,
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
        if priority is None:
            try:
                priority = int(os.environ.get(SITL_PRIORITY_ENV, "0"))
            except ValueError as exc:
                raise ValueError(f"{SITL_PRIORITY_ENV} must be an integer") from exc
        self.priority = int(priority)
        self.owner = self.path / "owner"
        self.acquired = False
        self.owner_text = ""
        self._ticket: Path | None = None
        self._heartbeat_stop: threading.Event | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._wait_started: float | None = None
        self._wait_count = 0

    @property
    def _queue_path(self) -> Path:
        return self.root_path.with_name(f"{self.root_path.name}.queue")

    def _make_ticket(self) -> Path:
        if self.slot is not None and not 0 <= self.slot < self.slots:
            raise ValueError(f"SITL slot outside configured range: {self.slot}")
        self._queue_path.mkdir(parents=True, exist_ok=True)
        # The timestamp gives FIFO ordering; the UUID makes creation atomic
        # across processes without a central counter.
        # Lower sort keys win; invert priority so larger values are served first.
        rank = max(0, min(999_999_999_999, 999_999_999_999 - self.priority))
        ticket = self._queue_path / (
            f"p{rank:012d}-{time.time_ns():020d}-{os.getpid()}-{uuid.uuid4().hex}"
        )
        ticket.write_text(
            f"{self.label} {os.getpid()} {datetime.now(timezone.utc).isoformat()}\n"
            + json.dumps({"sitl_slots": self.slots, "sitl_slot": self.slot}) + "\n",
            encoding="utf-8",
        )
        self._ticket = ticket
        return ticket

    def _reclaim_stale_tickets(self) -> None:
        try:
            tickets = list(self._queue_path.iterdir())
        except OSError:
            return
        for ticket in tickets:
            try:
                if time.time() - ticket.stat().st_mtime <= self.stale_s:
                    continue
                parsed = _parse_owner(ticket.read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                continue
            if parsed is None:
                continue
            pid, recorded_start = parsed
            alive, observed_start = _process_snapshot(pid)
            same_process = alive
            if recorded_start is not None and observed_start is not None:
                same_process = abs(recorded_start - observed_start) <= 1e-3
            if not same_process:
                ticket.unlink(missing_ok=True)

    def _ticket_is_eligible(self, slot: int) -> bool:
        """Honor priority/FIFO among waiters that can acquire this slot.

        Checking the actual candidate prevents a later general request from
        stealing an earlier fixed request's slot merely because other slots
        are free. Disjoint pools do not block one another.
        """
        if self._ticket is None:
            return True
        try:
            tickets = sorted(self._queue_path.iterdir(), key=lambda p: p.name)
        except OSError:
            return False
        for ticket in tickets:
            if ticket == self._ticket:
                return True
            scope = _ticket_candidates(ticket)
            if scope is None or (self.slots == 1 and bool(scope)) or slot in scope:
                return False
        return False

    def _start_heartbeat(self) -> None:
        self._heartbeat_stop = threading.Event()
        interval = max(0.05, min(30.0, self.stale_s / 3.0))

        def beat() -> None:
            while self._heartbeat_stop is not None and not self._heartbeat_stop.wait(interval):
                try:
                    os.utime(self.owner, None)
                except OSError:
                    return

        self._heartbeat_thread = threading.Thread(target=beat, name="sitl-lock-heartbeat", daemon=True)
        self._heartbeat_thread.start()

    def _stop_heartbeat(self) -> None:
        if self._heartbeat_stop is not None:
            self._heartbeat_stop.set()
        if self._heartbeat_thread is not None and self._heartbeat_thread is not threading.current_thread():
            self._heartbeat_thread.join(timeout=1.0)
        self._heartbeat_stop = None
        self._heartbeat_thread = None

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

    def _wait_for_retry(self, reason: str) -> None:
        """Report bounded lock waits so an outer job timeout remains diagnosable."""
        if self._wait_started is None:
            self._wait_started = time.monotonic()
        self._wait_count += 1
        if self._wait_count == 1 or self._wait_count % 10 == 0:
            print(
                json.dumps(
                    {
                        "event": "sitl_lock_waiting",
                        "reason": reason,
                        "lock": str(self.root_path),
                        "slots": self.slots,
                        "requested_slot": self.slot,
                        "queue": str(self._queue_path),
                        "wait_count": self._wait_count,
                        "elapsed_s": round(time.monotonic() - self._wait_started, 3),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                file=sys.stderr,
                flush=True,
            )
        time.sleep(self.retry_s)

    def acquire(self, wait: bool = True) -> None:
        if self.acquired:
            return
        if self._ticket is None:
            self._make_ticket()
        while True:
            self.root_path.parent.mkdir(parents=True, exist_ok=True)
            self._reclaim_stale_tickets()
            # A pre-fairness process owns the unsuffixed legacy directory.
            # Reclaim it only after the same heartbeat grace period, so a live
            # old worker remains visible and cannot be stolen.
            if self.slots > 1 and self.root_path.exists():
                previous_path = self.path
                previous_owner = self.owner
                self.path, self.owner = self.root_path, self.root_path / "owner"
                self._reclaim_stale()
                self.path, self.owner = previous_path, previous_owner
            if self.slots > 1 and self.root_path.exists():
                if not wait:
                    self._ticket.unlink(missing_ok=True)
                    self._ticket = None
                    raise RuntimeError(f"legacy SITL lock busy: {self.root_path}")
                self._wait_for_retry("legacy_lock_busy")
                continue
            candidates = (self.slot,) if self.slot is not None else range(self.slots)
            eligible = tuple(slot for slot in candidates if self._ticket_is_eligible(slot))
            if not eligible:
                if not wait:
                    self._ticket.unlink(missing_ok=True)
                    self._ticket = None
                    raise RuntimeError(f"SITL slots busy: {self.root_path}")
                self._wait_for_retry("queue_wait")
                continue
            acquired_path = None
            for slot in eligible:
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
                    self._ticket.unlink(missing_ok=True)
                    self._ticket = None
                    raise RuntimeError(f"SITL slots busy: {self.root_path}")
                self._wait_for_retry("slot_race")
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
                self._ticket.unlink(missing_ok=True) if self._ticket is not None else None
                self._ticket = None
                raise
            self.acquired = True
            self._ticket.unlink(missing_ok=True)
            self._ticket = None
            self._start_heartbeat()
            while not self._ready():
                if not wait:
                    self.release()
                    raise RuntimeError("SITL prerequisites are busy or memory is below threshold")
                self._wait_for_retry("prerequisites_busy")
            return

    def release(self) -> None:
        if not self.acquired:
            return
        self._stop_heartbeat()
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
