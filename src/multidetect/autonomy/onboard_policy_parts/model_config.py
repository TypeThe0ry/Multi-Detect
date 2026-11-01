"""Model identity and planner model configuration helpers."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _model_sha256(path: str | Path, *, cache_path: Path | None = None) -> str:
    """Hash a model with a size/mtime keyed cache; failures are non-fatal."""
    from scripts import onboard_policy

    path_type = onboard_policy.Path
    candidate = path_type(path)
    try:
        stat = candidate.stat()
        if not candidate.is_file():
            return "unknown"
    except OSError:
        return "unknown"
    key = str(candidate.resolve())
    cached: dict[str, Any] = {}
    if cache_path is not None:
        try:
            document = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(document, dict):
                cached = document
            entry = cached.get(key)
            if (
                isinstance(entry, dict)
                and entry.get("size") == stat.st_size
                and entry.get("mtime_ns") == stat.st_mtime_ns
                and isinstance(entry.get("sha256"), str)
            ):
                return entry["sha256"]
        except (OSError, ValueError, TypeError):
            cached = {}
    try:
        digest = onboard_policy.hashlib.sha256()
        with candidate.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        result = digest.hexdigest()
    except (OSError, ValueError):
        return "unknown"
    if cache_path is not None:
        cached[key] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": result}
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
            temporary.write_text(json.dumps(cached, sort_keys=True), encoding="utf-8")
            os.replace(temporary, cache_path)
        except (OSError, TypeError, ValueError):
            pass
    return result


def _planning_model_spec(value: Any, *, identity_mmproj: str) -> tuple[str, str | None] | None:
    """Normalize the optional planner model; planner prompts are text-only."""
    from scripts import onboard_policy

    if value is None or value == "":
        return None
    if isinstance(value, Mapping):
        model = value.get("gguf", value.get("model", value.get("path")))
        if model is None:
            raise ValueError("planning_model requires gguf/model/path")
        # The current mission-planning prompt is text-only, so a projector is
        # unnecessary.  A caller may still provide one for a future multimodal prompt.
        mmproj = value.get("mmproj")
    else:
        model, mmproj = value, None
    path_type = onboard_policy.Path
    model_path = path_type(str(model))
    model_path = model_path if model_path.is_absolute() else onboard_policy.ROOT / model_path
    if mmproj is not None:
        mmproj_path = path_type(str(mmproj))
        mmproj = str(
            mmproj_path if mmproj_path.is_absolute() else onboard_policy.ROOT / mmproj_path
        )
    return str(model_path), mmproj
