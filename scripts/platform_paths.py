"""Small platform path helpers shared by simulator entry points."""

from __future__ import annotations

import os
from pathlib import Path


def default_llama_server(root: Path, *, prefer_tools: bool = False) -> Path:
    """Return the local llama-server path, honoring ``LLAMA_SERVER`` first."""

    configured = os.environ.get("LLAMA_SERVER")
    if configured:
        return Path(configured).expanduser()
    cache = root / "artifacts/cache/llama-cpp/b11552"
    candidates = (
        (root / "tools/llama.cpp/llama-server.exe", cache / "llama-server.exe", cache / "llama-server")
        if os.name == "nt" and prefer_tools
        else (cache / "llama-server.exe", cache / "llama-server")
        if os.name == "nt"
        else (cache / "llama-server", cache / "llama-server.exe")
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    if os.name == "nt" and prefer_tools:
        return root / "tools/llama.cpp/llama-server.exe"
    return cache / ("llama-server.exe" if os.name == "nt" else "llama-server")


def pythonpath_value(root: Path) -> str:
    """Build a platform-correct ``PYTHONPATH`` for child processes."""

    return os.pathsep.join((str(root / "src"), str(root)))
