"""Bounded per-action video capture for SIM teacher episodes."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any


class ContinuousVideoWriter:
    def __init__(self, episode_dir: str | Path, *, hz: float, max_bytes: int) -> None:
        if hz <= 0 or max_bytes <= 0:
            raise ValueError("video rate and byte cap must be positive")
        self.root = Path(episode_dir) / "video"
        self.rgb_dir = self.root / "rgb"
        self.seg_dir = self.root / "seg"
        self.depth_dir = self.root / "depth"
        for directory in (self.rgb_dir, self.seg_dir, self.depth_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "manifest.jsonl"
        self.hz = float(hz)
        self.max_bytes = int(max_bytes)
        self.bytes_written = 0
        self.frames_written = 0
        self.truncated = False
        self.started_at_s: float | None = None
        self._last_stamp_ns: int | None = None

    def write(self, observation: dict[str, Any], *, action_index: int) -> bool:
        if self.truncated:
            return False
        import numpy as np
        from PIL import Image

        stamp_ns = int(observation["frame_sensor_stamp_ns"])
        if self._last_stamp_ns == stamp_ns:
            return False
        stem = f"{self.frames_written:07d}_{stamp_ns}"
        raw_rgb = observation["rgb"]
        if isinstance(raw_rgb, (bytes, bytearray, memoryview)):
            rgb = np.asarray(Image.open(io.BytesIO(bytes(raw_rgb))).convert("RGB"), dtype=np.uint8)
        else:
            rgb = np.asarray(raw_rgb, dtype=np.uint8)
        rgb_path = self.rgb_dir / f"{stem}.jpg"
        Image.fromarray(rgb, mode="RGB").save(rgb_path, format="JPEG", quality=88, optimize=True)
        seg_path = None
        if observation.get("segmentation") is not None:
            seg_path = self.seg_dir / f"{stem}.png"
            Image.fromarray(
                np.asarray(observation["segmentation"], dtype=np.uint8), mode="RGB"
            ).save(seg_path, format="PNG", optimize=True)
        depth_path = None
        if observation.get("depth") is not None:
            depth_path = self.depth_dir / f"{stem}.npz"
            np.savez_compressed(
                depth_path, depth=np.asarray(observation["depth"], dtype=np.float32)
            )
        paths = [p for p in (rgb_path, seg_path, depth_path) if p is not None]
        size = sum(path.stat().st_size for path in paths)
        if self.bytes_written + size > self.max_bytes:
            for path in paths:
                path.unlink(missing_ok=True)
            self.truncated = True
            self._write_manifest(
                {
                    "event": "truncated",
                    "reason": "max_video_bytes",
                    "bytes_written": self.bytes_written,
                }
            )
            return False
        elapsed = None if self.started_at_s is None else stamp_ns / 1e9 - self.started_at_s
        if self.started_at_s is None:
            self.started_at_s = stamp_ns / 1e9
        row = {
            "frame_index": self.frames_written,
            "action_index": int(action_index),
            "sensor_stamp_ns": stamp_ns,
            "captured_at_s": stamp_ns / 1e9,
            "rgb_path": rgb_path.relative_to(self.root).as_posix(),
            "segmentation_path": seg_path.relative_to(self.root).as_posix() if seg_path else None,
            "depth_path": depth_path.relative_to(self.root).as_posix() if depth_path else None,
            "depth_encoding": "FLOAT32_M",
            "camera_info": observation.get("camera_info"),
            "gimbal_state": observation.get("gimbal_state"),
            "camera_pose": observation.get("camera_pose"),
            "pose": observation.get("pose"),
            "attitude": observation.get("attitude"),
            "velocity": observation.get("velocity"),
            "pose_time_boot_ms": observation.get("pose_time_boot_ms"),
            "sim_skew_ms": observation.get("sim_skew_ms"),
            "flight_stage": observation.get("flight_stage", "action"),
            "elapsed_video_s": elapsed,
            "bytes": size,
        }
        self._write_manifest(row)
        self.frames_written += 1
        self.bytes_written += size
        self._last_stamp_ns = stamp_ns
        return True

    def close(self) -> dict[str, Any]:
        manifest = {
            "schema_version": 1,
            "sampling_hz": self.hz,
            "frame_count": self.frames_written,
            "bytes_written": self.bytes_written,
            "max_bytes": self.max_bytes,
            "truncated": self.truncated,
            "estimated_bytes_per_minute_at_sampling_rate": (
                round(self.bytes_written / max(1, self.frames_written) * self.hz * 60)
                if self.frames_written
                else None
            ),
            "frame_manifest": "manifest.jsonl",
        }
        (self.root / "video.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return manifest

    def _write_manifest(self, row: dict[str, Any]) -> None:
        with self.manifest_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


__all__ = ["ContinuousVideoWriter"]
