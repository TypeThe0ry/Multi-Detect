"""Small, model-free resource guards shared by round and episode runners."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from pathlib import Path


def ensure_free_disk(path: Path, min_free_gb: float) -> None:
    probe = path.resolve()
    while not probe.exists():
        probe = probe.parent
    free = shutil.disk_usage(probe).free / 1_000_000_000
    if free < min_free_gb:
        raise RuntimeError(f"Disk guard: {free:.2f} GB free at {probe}; need {min_free_gb:g} GB")


class GPULock:
    """Exclusive creation, never steal a lock (including when --force is used).

    Training and other evaluation entry points must use this same path/protocol.
    A stale lock requires operator inspection/removal, rather than unsafe PID reuse.
    """

    def __init__(self, path: Path, model: str):
        self.path, self.model = path, model

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise RuntimeError(
                f"GPU lock held: {self.path}; inspect owner before removing"
            ) from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {"pid": os.getpid(), "host": socket.gethostname(), "model": self.model}, handle
                )
        except BaseException:
            self.path.unlink(missing_ok=True)
            raise
        return self

    def __exit__(self, *_):
        self.path.unlink()


def check_gpu(*, force: bool = False, heavy_mib: int = 512, runner=None) -> None:
    if force:
        return
    runner = runner or subprocess.run
    try:
        result = runner(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,process_name,used_gpu_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        heavy = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            pid, name, memory = [part.strip() for part in line.split(",", 2)]
            # WDDM may report N/A: unknown compute consumers are unsafe too.
            if not memory.isdigit() or int(memory) >= heavy_mib:
                heavy.append(f"pid={pid} {name} ({memory} MiB)")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise RuntimeError(
            f"Cannot verify GPU availability: {exc}; --force overrides GPU inspection only"
        ) from exc
    if heavy:
        raise RuntimeError("Other heavy GPU processes: " + "; ".join(heavy))
