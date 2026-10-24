"""Small, dependency-free provenance records for SIM and regression artifacts."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable


def _sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, ValueError):
        return None


def _model_hash(path: Path, cache_path: Path | None) -> str | None:
    try:
        stat = path.stat()
        if not path.is_file():
            return None
    except OSError:
        return None
    key = str(path.resolve())
    cache: dict[str, Any] = {}
    if cache_path:
        try:
            value = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                cache = value
        except (OSError, ValueError, TypeError):
            pass
        entry = cache.get(key)
        if isinstance(entry, dict) and entry.get("size") == stat.st_size and entry.get("mtime_ns") == stat.st_mtime_ns:
            if isinstance(entry.get("sha256"), str):
                return entry["sha256"]
    result = _sha256(path)
    if result and cache_path:
        cache[key] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": result}
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass
    return result


def _git(root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"available": False, "worktree_root": str(root.resolve())}
    try:
        def run(*args: str) -> str:
            return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL, text=True).strip()
        top = Path(run("rev-parse", "--show-toplevel")).resolve()
        full = run("rev-parse", "HEAD")
        branch = run("branch", "--show-current")
        dirty = run("status", "--porcelain")
        result.update({"available": True, "commit": full, "short_commit": full[:12],
                       "dirty": bool(dirty), "changed_files": len(dirty.splitlines()) if dirty else 0,
                       "branch": branch or None, "detached": not bool(branch), "worktree_root": str(top)})
    except (OSError, subprocess.SubprocessError, ValueError):
        result.update({"commit": None, "short_commit": None, "dirty": None, "changed_files": None,
                       "branch": None, "detached": None})
    return result


def build_provenance(*, root: str | Path | None = None, config_paths: Iterable[str | Path] = (),
                     model_paths: Iterable[str | Path] = (), world_paths: Iterable[str | Path] = (),
                     command: Iterable[str] | None = None, model_cache: str | Path | None = None) -> dict[str, Any]:
    root_path = Path(root or Path.cwd()).resolve()
    cache = Path(model_cache).resolve() if model_cache else root_path / ".model-hash-cache.json"
    def files(paths: Iterable[str | Path], *, model: bool = False) -> list[dict[str, Any]]:
        out = []
        for raw in paths:
            path = Path(raw).expanduser().resolve()
            item = {"path": str(path), "sha256": _model_hash(path, cache) if model else _sha256(path)}
            if model:
                try: item.update({"size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns})
                except OSError: pass
            out.append(item)
        return out
    return {"code": _git(root_path), "config": files(config_paths), "models": files(model_paths, model=True),
            "worlds": files(world_paths), "command": list(command) if command is not None else list(sys.argv)}


def snapshot_config(path: str | Path | None, destination: str | Path) -> dict[str, Any] | None:
    if not path:
        return None
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        return {"path": str(source), "sha256": None, "snapshot": None}
    target = Path(destination) / source.name
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    except OSError:
        target = None
    return {"path": str(source), "sha256": _sha256(source), "snapshot": str(target) if target else None}


def model_paths_from_config(path: str | Path | None) -> list[Path]:
    """Find model-like paths in an onboard JSON without requiring its schema."""
    if not path:
        return []
    try:
        value: Any = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    found: list[Path] = []
    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if isinstance(child, str) and ("model" in key.lower() or key.lower() in {"gguf", "mmproj", "weights", "weight_path"}):
                    found.append(Path(child))
                else:
                    walk(child)
        elif isinstance(item, list):
            for child in item: walk(child)
    walk(value)
    return found


def replay_version_record(recorded: dict[str, Any] | None, current: dict[str, Any] | None = None) -> dict[str, Any]:
    current = current or _git(Path.cwd())
    recorded = recorded or {}
    old, new = recorded.get("commit"), current.get("commit")
    return {"recorded_code": recorded or None, "current_code": current,
            "version_mismatch": bool(old and new and old != new)}
