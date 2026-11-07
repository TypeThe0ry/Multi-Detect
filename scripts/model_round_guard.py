"""Small, model-free resource guards shared by round and episode runners."""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

try:  # Optional dependency; ctypes/procfs fallbacks keep the guard standalone.
    import psutil  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised on minimal hosts
    psutil = None  # type: ignore[assignment]


LOGGER = logging.getLogger(__name__)


def _process_snapshot(pid: int) -> tuple[bool, float | None]:
    """Return ``(alive, process creation time)`` for *pid*.

    Process creation time is wall-clock seconds since the Unix epoch.  It is
    used to distinguish a reused PID from the process that created a lock.
    The fallback paths deliberately return ``None`` when creation time cannot
    be queried; in that case liveness still protects us from deleting a live
    lock.
    """

    if pid <= 0:
        return False, None
    if psutil is not None:
        try:
            process = psutil.Process(pid)
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return False, None
            return True, float(process.create_time())
        except psutil.NoSuchProcess:
            return False, None
        except (psutil.AccessDenied, PermissionError):
            return True, None
        except (OSError, ValueError):
            return False, None

    if os.name == "nt":
        return _windows_process_snapshot(pid)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, None
    except PermissionError:
        return True, None
    except OSError:
        return False, None
    return True, _procfs_process_start(pid)


def _procfs_process_start(pid: int) -> float | None:
    """Read Linux process start time when psutil is unavailable."""

    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # The comm field may contain spaces and parentheses.  Everything after
        # its final ')' begins with field 3 (state), so field 22 is index 19.
        fields = stat.rsplit(")", 1)[1].split()
        ticks = int(fields[19])
        btime = next(
            int(line.split()[1])
            for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines()
            if line.startswith("btime ")
        )
        return btime + ticks / float(os.sysconf("SC_CLK_TCK"))
    except (OSError, StopIteration, IndexError, ValueError, TypeError):
        return None


def _windows_process_snapshot(pid: int) -> tuple[bool, float | None]:
    """Use the Win32 process APIs without spawning a shell or helper process."""

    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_INVALID_PARAMETER = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        if ctypes.get_last_error() == ERROR_INVALID_PARAMETER:
            return False, None
        return True, None

    class _FileTime(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    creation, _exit, _kernel, _user = (_FileTime(), _FileTime(), _FileTime(), _FileTime())
    try:
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(_exit),
            ctypes.byref(_kernel),
            ctypes.byref(_user),
        )
        if not ok:
            return True, None
        # FILETIME is 100-ns intervals since 1601-01-01; convert to Unix time.
        value = (int(creation.high) << 32) | int(creation.low)
        return True, value / 10_000_000 - 11644473600
    finally:
        kernel32.CloseHandle(handle)


def _remove_stale_lock(path: Path) -> bool:
    """Remove a stale lock owned by this host, returning whether it was removed."""

    try:
        owner: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if owner.get("host") != socket.gethostname():
        return False
    try:
        pid = int(owner["pid"])
    except (KeyError, TypeError, ValueError):
        return False

    alive, observed_start = _process_snapshot(pid)
    reason: str | None = None
    if not alive:
        reason = f"pid {pid} is no longer alive"
    else:
        recorded_start = owner.get("start_time")
        if recorded_start is not None and observed_start is not None:
            try:
                # Process APIs can differ by a few microseconds in rounding.
                if abs(float(recorded_start) - observed_start) > 1e-3:
                    reason = f"pid {pid} has a different process start time"
            except (TypeError, ValueError):
                return False
    if reason is None:
        return False

    try:
        path.unlink()
    except FileNotFoundError:
        return True
    LOGGER.warning("Removing stale GPU lock %s: %s", path, reason)
    return True


def ensure_free_disk(path: Path, min_free_gb: float) -> None:
    probe = path.resolve()
    while not probe.exists():
        probe = probe.parent
    free = shutil.disk_usage(probe).free / 1_000_000_000
    if free < min_free_gb:
        raise RuntimeError(f"Disk guard: {free:.2f} GB free at {probe}; need {min_free_gb:g} GB")


class GPULock:
    """Exclusive creation with conservative stale-owner recovery.

    Training and other evaluation entry points must use this same path/protocol.
    ``--force`` only applies to GPU inspection and never bypasses a live lock.
    """

    def __init__(self, path: Path, model: str):
        self.path, self.model = path, model

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            if not _remove_stale_lock(self.path):
                raise RuntimeError(
                    f"GPU lock held: {self.path}; inspect owner before removing"
                ) from exc
            # A stale owner was removed.  Creation remains O_EXCL so a racing
            # evaluator wins safely and this process refuses its live lock.
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError as retry_exc:
                raise RuntimeError(
                    f"GPU lock held: {self.path}; inspect owner before removing"
                ) from retry_exc
        alive, process_start = _process_snapshot(os.getpid())
        # The current process must be alive; use wall time only on unusual hosts
        # where no platform process-start API is available.
        start_time = process_start if alive and process_start is not None else time.time()
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "pid": os.getpid(),
                        "host": socket.gethostname(),
                        "start_time": start_time,
                        "model": self.model,
                    },
                    handle,
                )
        except BaseException:
            self.path.unlink(missing_ok=True)
            raise
        return self

    def __exit__(self, *_):
        self.path.unlink()


def _visible_device_args() -> list[str]:
    """``nvidia-smi -i`` arguments for numeric CUDA_VISIBLE_DEVICES, else all devices."""

    visible = os.environ.get("CUDA_VISIBLE_DEVICES", "").strip()
    indices = [part.strip() for part in visible.split(",") if part.strip()]
    if not indices or not all(part.isdigit() for part in indices):
        return []
    return ["-i", ",".join(indices)]


def check_gpu(*, force: bool = False, heavy_mib: int = 512, idle_device_mib: int = 1536, runner=None) -> None:
    if force:
        return
    runner = runner or subprocess.run
    # Only inspect the devices this process will use, so parallel jobs pinned
    # to other GPUs via CUDA_VISIBLE_DEVICES do not block each other.
    select = _visible_device_args()
    try:
        result = runner(
            [
                "nvidia-smi",
                *select,
                "--query-compute-apps=pid,process_name,used_gpu_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        heavy, unknown = [], []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            pid, name, memory = [part.strip() for part in line.split(",", 2)]
            if not memory.isdigit():
                unknown.append(f"pid={pid} {name} ({memory} MiB)")
            elif int(memory) >= heavy_mib:
                heavy.append(f"pid={pid} {name} ({memory} MiB)")
        if unknown:
            # Windows WDDM reports N/A per process for every desktop app that
            # touches the GPU (explorer, terminals, browsers).  Those are only
            # unsafe if the device as a whole is busy, so fall back to the
            # device-wide figure; if that cannot be read, stay conservative.
            device_used = None
            try:
                device = runner(
                    [
                        "nvidia-smi",
                        *select,
                        "--query-gpu=memory.used",
                        "--format=csv,noheader,nounits",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                values = [line.strip() for line in device.stdout.splitlines() if line.strip()]
                if values and all(value.isdigit() for value in values):
                    device_used = max(int(value) for value in values)
            except (OSError, subprocess.SubprocessError):
                device_used = None
            if device_used is None or device_used >= idle_device_mib:
                heavy.extend(unknown)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise RuntimeError(
            f"Cannot verify GPU availability: {exc}; --force overrides GPU inspection only"
        ) from exc
    if heavy:
        raise RuntimeError("Other heavy GPU processes: " + "; ".join(heavy))
