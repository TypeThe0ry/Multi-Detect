"""Small helpers for host-specific paths used by repository scripts."""

from __future__ import annotations

import os
from pathlib import Path


def host_path(env: str, windows: str, posix: Path, *, platform: str | None = None) -> Path:
    """Use an environment override, else preserve Windows or use a repo-local path."""

    value = os.environ.get(env)
    if value:
        return Path(value)
    is_windows = os.name == "nt" if platform is None else platform == "nt"
    return Path(windows) if is_windows else Path(posix)


def executable_name(name: str, *, platform: str | None = None) -> str:
    """Return a native executable filename for the selected host platform."""

    is_windows = os.name == "nt" if platform is None else platform == "nt"
    stem = name.removesuffix(".exe")
    return f"{stem}.exe" if is_windows else stem
