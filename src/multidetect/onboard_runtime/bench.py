"""Latency-only backends and an explicitly synthetic recorded-format fixture."""

from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path
from typing import Any

from multidetect.autonomy.onboard_loop import Detection


class LatencyProposer:
    def __init__(self, latency_s: float = 0.07) -> None:
        if not math.isfinite(latency_s) or latency_s < 0:
            raise ValueError("detector latency must be finite and nonnegative")
        self.latency_s = latency_s
        self.calls = 0
        self.stopped = threading.Event()

    def __call__(self, bgr) -> list[Detection]:
        if self.stopped.wait(self.latency_s):
            raise RuntimeError("bench detector stopped")
        self.calls += 1
        height, width = bgr.shape[:2]
        return [Detection((width * 0.45, height * 0.55, width * 0.55, height * 0.65), 0.9)]


class LatencySlowRunner:
    last_logprobs = None

    def __init__(self, latency_s: float = 1.5) -> None:
        if not math.isfinite(latency_s) or latency_s < 0:
            raise ValueError("slow latency must be finite and nonnegative")
        self.latency_s = latency_s
        self.calls = 0
        self.stopped = threading.Event()

    def __call__(self, row: dict[str, Any]) -> tuple[str, float]:
        started = time.monotonic()
        if self.stopped.wait(self.latency_s):
            raise RuntimeError("bench slow backend stopped")
        self.calls += 1
        return json.dumps({
            "decision": "approach_track",
            "args": {"track_id": row["focus_track_id"], "region": None, "duration_s": None},
            "reason": "Synthetic latency-only bench verdict; not an identity result.",
        }), time.monotonic() - started

    def close(self) -> None:
        self.stopped.set()


def generate_fixture(path: Path, *, duration_s: float = 8.0, fps: float = 10.0) -> Path:
    """Write test inputs only; this does not run a simulator or a model."""
    from PIL import Image

    if (not math.isfinite(duration_s) or not math.isfinite(fps)
        or duration_s <= 0 or fps <= 0):
        raise ValueError("fixture duration and fps must be positive and finite")
    if path.exists():
        raise FileExistsError(f"fixture destination already exists: {path}")
    (path / "rgb").mkdir(parents=True)
    Image.new("RGB", (160, 120), color=(40, 70, 90)).save(path / "rgb" / "fixture.png")
    info = {"width": 160, "height": 120, "fx": 120.0, "fy": 120.0,
            "cx": 80.0, "cy": 60.0}
    origin = 10_000_000_000
    with (path / "steps.jsonl").open("w", encoding="utf-8") as steps, (
        path / "mavlink.jsonl"
    ).open("w", encoding="utf-8") as telemetry:
        for index in range(int(duration_s * fps) + 1):
            stamp = origin + int(index / fps * 1e9)
            steps.write(json.dumps({
                "step_index": index, "receive_monotonic_ns": stamp,
                "obs_before": {"rgb_path": "rgb/fixture.png", "camera_info": info,
                               "frame_received_monotonic_ns": stamp},
            }) + "\n")
            messages = [
                {"mavpackettype": "HEARTBEAT", "base_mode": 128,
                 "custom_mode": 6 << 16, "autopilot": 12},
                {"mavpackettype": "LOCAL_POSITION_NED", "x": 0, "y": 0, "z": -10,
                 "vx": 0, "vy": 0, "vz": 0},
                {"mavpackettype": "ATTITUDE", "roll": 0, "pitch": 0, "yaw": 0},
                {"mavpackettype": "HOME_POSITION", "x": 0, "y": 0, "z": 0,
                 "latitude": 0, "longitude": 0, "altitude": 0},
                {"mavpackettype": "EXTENDED_SYS_STATE", "landed_state": 2},
            ]
            for message in messages:
                telemetry.write(json.dumps({**message, "receive_monotonic_ns": stamp}) + "\n")
    (path / "episode.json").write_text(json.dumps({
        "task_text": "Find the bench object and record its position.",
        "sensor_status": "synthetic_bench", "synthetic": True,
    }, indent=2), encoding="utf-8")
    return path
