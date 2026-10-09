"""Append-only episode recorder and offline dataset auditor."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Any


def _json_default(value: Any):
    if hasattr(value, "tolist"):
        return value.tolist()
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


class EpisodeRecorder:
    def __init__(self, root: str | Path, episode_id: str, *, task_text: str = ""):
        self.episode_dir = Path(root) / "episodes" / episode_id
        self.rgb_dir = self.episode_dir / "rgb"
        self.depth_dir = self.episode_dir / "depth"
        self.rgb_dir.mkdir(parents=True, exist_ok=True)
        self.depth_dir.mkdir(exist_ok=True)
        self.steps_path = self.episode_dir / "steps.jsonl"
        self.mavlink_path = self.episode_dir / "mavlink.jsonl"
        self.episode_path = self.episode_dir / "episode.json"
        self.index = 0
        self.meta = {
            "schema_version": 1,
            "episode_id": episode_id,
            "task_text": task_text,
            "created_wall_time": time.time(),
            "step_count": 0,
        }
        self.episode_path.write_text(json.dumps(self.meta, indent=2), encoding="utf-8")

    def _observation(self, obs: dict[str, Any] | None, prefix: str) -> dict[str, Any] | None:
        if obs is None:
            return None
        out = {k: v for k, v in obs.items() if k not in {"rgb", "depth"}}
        stem = f"{self.index:06d}_{prefix}"
        rgb = obs.get("rgb")
        if rgb is not None:
            path = self.rgb_dir / f"{stem}.png"
            if isinstance(rgb, (bytes, bytearray)):
                path.write_bytes(bytes(rgb))
            elif isinstance(rgb, str):
                path.write_bytes(base64.b64decode(rgb))
            else:
                try:
                    from PIL import Image

                    Image.fromarray(rgb).save(path)
                except Exception:
                    path.write_bytes(json.dumps(rgb, default=_json_default).encode())
            out["rgb_path"] = str(path.relative_to(self.episode_dir))
        depth = obs.get("depth")
        if depth is not None:
            path = self.depth_dir / f"{stem}.npy"
            try:
                import numpy as np

                np.save(path, np.asarray(depth, dtype="float32"))
            except Exception:
                path.write_text(json.dumps(depth, default=_json_default), encoding="utf-8")
            out["depth_path"] = str(path.relative_to(self.episode_dir))
        out.setdefault("receive_monotonic_ns", time.monotonic_ns())
        out.setdefault("receive_wall_time", time.time())
        return out

    def record_step(
        self,
        obs_before: dict[str, Any],
        action: Any,
        *,
        ack: dict[str, Any] | None = None,
        outcome: dict[str, Any] | None = None,
        obs_after: dict[str, Any] | None = None,
        labels: dict[str, Any] | None = None,
    ) -> None:
        before = self._observation(obs_before, "before")
        after = self._observation(obs_after, "after") if obs_after else None
        action_obj = action.to_dict() if hasattr(action, "to_dict") else action
        row = {
            "step_index": self.index,
            "task_text": self.meta["task_text"],
            "receive_monotonic_ns": time.monotonic_ns(),
            "receive_wall_time": time.time(),
            "obs_before": before,
            "action": action_obj,
            "ack": ack or {},
            "outcome": outcome or {},
            "obs_after": after,
            "labels": labels or {},
        }
        with self.steps_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=_json_default) + "\n")
        self.index += 1
        self.meta["step_count"] = self.index
        self.episode_path.write_text(json.dumps(self.meta, indent=2), encoding="utf-8")

    def record_mavlink(self, message: Any) -> None:
        row = message if isinstance(message, dict) else {"message": str(message)}
        row = {**row, "receive_monotonic_ns": time.monotonic_ns(), "receive_wall_time": time.time()}
        with self.mavlink_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=_json_default) + "\n")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class DatasetAuditError(ValueError):
    pass


def audit_dataset(root: str | Path) -> dict[str, Any]:
    root = Path(root)
    episodes = root / "episodes"
    manifest: dict[str, str] = {}
    total_steps = 0
    successes = 0
    action_hist: dict[str, int] = {}
    skews: list[float] = []
    if not episodes.is_dir():
        raise DatasetAuditError("episodes directory missing")
    for file in sorted(p for p in episodes.rglob("*") if p.is_file()):
        manifest[file.relative_to(root).as_posix()] = _sha256(file)
    # Re-running an audit must detect bytes changed after a prior report.
    previous_path = root / "audit.json"
    if previous_path.is_file():
        try:
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
            previous_manifest = previous.get("files", {})
        except (OSError, json.JSONDecodeError) as exc:
            raise DatasetAuditError("existing audit.json is not valid JSON") from exc
        if not isinstance(previous_manifest, dict):
            raise DatasetAuditError("existing audit.json has no file manifest")
        if previous_manifest != manifest:
            changed = sorted(set(previous_manifest) | set(manifest), key=str)
            changed = [
                item for item in changed if previous_manifest.get(item) != manifest.get(item)
            ]
            raise DatasetAuditError(
                "dataset changed since previous audit: " + ", ".join(changed[:8])
            )
    for ep in sorted(p for p in episodes.iterdir() if p.is_dir()):
        steps_file = ep / "steps.jsonl"
        if not steps_file.exists():
            raise DatasetAuditError(f"missing steps.jsonl: {ep.name}")
        prev = -1
        previous_receive_ns = -1
        for line_no, line in enumerate(steps_file.read_text(encoding="utf-8").splitlines(), 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetAuditError(f"invalid JSON {steps_file}:{line_no}") from exc
            idx = row.get("step_index")
            receive_ns = row.get("receive_monotonic_ns")
            if not isinstance(idx, int) or idx <= prev:
                raise DatasetAuditError(f"non-monotonic step index in {ep.name}")
            if isinstance(receive_ns, bool) or not isinstance(receive_ns, int):
                raise DatasetAuditError(f"invalid receive time in {ep.name}")
            if receive_ns <= previous_receive_ns:
                raise DatasetAuditError(f"non-monotonic receive time in {ep.name}")
            prev, previous_receive_ns = idx, receive_ns
            total_steps += 1
            action = row.get("action")
            if not isinstance(action, dict):
                raise DatasetAuditError(f"step {idx} in {ep.name} has no action")
            typ = action.get("kind", "unknown")
            action_hist[typ] = action_hist.get(typ, 0) + 1
            if (row.get("outcome") or {}).get("status") == "success":
                successes += 1
            if not isinstance(row.get("obs_before"), dict):
                raise DatasetAuditError(f"step {idx} in {ep.name} has no obs_before")
            b, a = row["obs_before"], row.get("obs_after") or {}
            for ob in (b, a):
                for key in ("rgb_path", "depth_path"):
                    if key in ob and not (ep / ob[key]).exists():
                        raise DatasetAuditError(f"missing referenced file {ob[key]}")
            if b.get("receive_monotonic_ns") and row.get("receive_monotonic_ns"):
                skews.append(abs(row["receive_monotonic_ns"] - b["receive_monotonic_ns"]) / 1e9)
    audit = {
        "files": manifest,
        "step_count": total_steps,
        "success_rate": successes / total_steps if total_steps else 0.0,
        "action_histogram": action_hist,
        "frame_pose_skew_s": {
            "count": len(skews),
            "max": max(skews, default=0.0),
            "mean": sum(skews) / len(skews) if skews else 0.0,
        },
    }
    previous_path.write_text(
        json.dumps(audit, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return audit


__all__ = ["EpisodeRecorder", "DatasetAuditError", "audit_dataset"]
