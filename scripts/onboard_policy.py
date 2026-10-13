"""Truth-free onboard policy for the SIM teacher collector (``--teacher onboard``).

FAST: a fine-tuned YOLOE model with a generic open vocabulary proposes candidate
boxes at low confidence (class labels are ignored). SLOW: the quantized
Qwen3-VL slow system (llama-server) judges whether a tracked candidate is the
task's object. The host loop (``multidetect.autonomy.onboard_loop``) decides
approach, confirmation by apparent size, and the return home. The policy only
receives the task text, camera frames, the vehicle pose and the static world
map used for search legs and obstacle clearance; it never reads scenario truth.
"""

from __future__ import annotations

import io
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.onboard_decision import identity_of  # noqa: E402
from multidetect.autonomy.onboard_loop import (  # noqa: E402
    CameraModel,
    Detection,
    LoopConfig,
    OnboardLoop,
)
from multidetect.sim_teacher.policies import (  # noqa: E402
    ExpertSearchPolicy,
    load_sdf_obstacles,
    segment_clear_of_obstacles,
)
from multidetect.simulator.camera_projection import pixel_ground_point  # noqa: E402

DEFAULTS = {
    "yoloe_weights": ROOT / "artifacts/experiments/yoloe-v2-r3-seg-drop-full/train/weights/best.pt",
    "gguf": ROOT
    / "artifacts/models/slow-system-lora-v2-r2-seg-merged-gguf"
    / "slow-system-v2r2seg-merged-Q5_K_M.gguf",
    "mmproj": ROOT
    / "artifacts/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/"
    "52d6c8ffea26cc873ac5ad116f8631268d7eb503/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf",
    "llama_server": ROOT / "artifacts/cache/llama-cpp/b11552/llama-server.exe",
}
# Generic proposal vocabulary: broad object words, not the task's answer.
VOCABULARY = (
    "car",
    "SUV",
    "pickup truck",
    "truck",
    "bus",
    "ambulance",
    "fire truck",
    "person",
    "traffic cone",
    "barrel",
    "concrete barrier",
    "fire hydrant",
    "dumpster",
    "electrical box",
    "mailbox",
    "post box",
    "stop sign",
    "cardboard box",
    "backpack",
    "sign",
)
MEMORY = "No prior confirmed target; search in progress."
MAX_PROMPT_CANDIDATES = 4


class YoloeProposer:
    """Class-agnostic proposals from YOLOE with a fixed generic vocabulary."""

    def __init__(self, weights: Path, *, confidence: float, imgsz: int) -> None:
        os.environ.setdefault("YOLO_OFFLINE", "true")
        os.environ.setdefault("YOLO_AUTOINSTALL", "false")
        from ultralytics import YOLOE

        self.model = YOLOE(str(weights))
        prompts = list(VOCABULARY)
        # Encode prompts before the first fp16 predict fuses the model.
        self.model.set_classes(prompts, self.model.get_text_pe(prompts))
        self.confidence, self.imgsz = confidence, imgsz

    def __call__(self, bgr) -> list[Detection]:
        result = self.model.predict(
            bgr, imgsz=self.imgsz, conf=self.confidence, device=0, verbose=False
        )[0]
        boxes = result.boxes
        return [
            Detection(tuple(float(v) for v in box), float(score), self.model.names[int(cls)])
            for box, score, cls in zip(
                boxes.xyxy.tolist(), boxes.conf.tolist(), boxes.cls.tolist(), strict=True
            )
        ]


class OnboardPolicy:
    def __init__(
        self,
        *,
        task_text: str,
        world_path: Path | None,
        altitude_m: float,
        camera_info: dict[str, float],
        image_size: tuple[int, int],
        work_dir: Path,
        config: dict[str, Any] | None = None,
    ) -> None:
        options = {**{k: str(v) for k, v in DEFAULTS.items()}, **(config or {})}
        self.task_text = task_text
        self.work_dir = work_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.image_size = image_size
        obstacles = load_sdf_obstacles(world_path) if world_path else ()
        # The search pattern is target-blind; a target placed far outside the
        # detection range keeps ExpertSearchPolicy in its search/scan states.
        self._search = ExpertSearchPolicy(
            target_ned=(1.0e7, 1.0e7, 0.0),
            intrinsics=camera_info,
            image_size=image_size,
            altitude_m=altitude_m,
            obstacles=obstacles,
        )
        camera = CameraModel(
            fx=float(camera_info["fx"]),
            fy=float(camera_info["fy"]),
            cx=float(camera_info["cx"]),
            cy=float(camera_info["cy"]),
            width=image_size[0],
            height=image_size[1],
        )
        self.loop = OnboardLoop(
            camera=camera,
            search_action=lambda pose, yaw: self._search.next_action(pose_ned=pose, yaw_deg=yaw),
            segment_clear=lambda pose, n, e: segment_clear_of_obstacles(
                pose, (n, e, pose[2]), max(0.0, -float(pose[2])), obstacles
            ),
            config=LoopConfig(confirm_min_side_px=float(options.get("confirm_min_side_px", 20.0))),
        )
        self.proposer = YoloeProposer(
            Path(options["yoloe_weights"]),
            confidence=float(options.get("confidence", 0.05)),
            imgsz=int(options.get("imgsz", 1280)),
        )
        from scripts.eval_slow_system import LlamaServerRunner

        self.slow = LlamaServerRunner(
            Path(options["gguf"]),
            Path(options["mmproj"]),
            executable=Path(options["llama_server"]),
            port=int(options.get("port", 9941)),
            crop_policy="candidate_zoom_v2",
        )
        self.slow_calls = 0

    def close(self) -> None:
        self.slow.close()

    def _slow_identity(self, frame_path: Path, current, focus) -> dict[str, Any]:
        """Ask the slow system about ``focus``; candidates are renamed track-N as trained."""
        shown = [focus] + [t for t in current if t.track_id != focus.track_id]
        shown = shown[:MAX_PROMPT_CANDIDATES]
        width, height = self.image_size
        names = {t.track_id: f"track-{index + 1}" for index, t in enumerate(shown)}
        row = {
            "image_path": str(frame_path),
            "task_text": self.task_text,
            "memory": MEMORY,
            "candidates": [
                {
                    "track_id": names[t.track_id],
                    "box_xyxy_normalized": [
                        round(min(1.0, max(0.0, t.bbox[0] / width)), 4),
                        round(min(1.0, max(0.0, t.bbox[1] / height)), 4),
                        round(min(1.0, max(0.0, t.bbox[2] / width)), 4),
                        round(min(1.0, max(0.0, t.bbox[3] / height)), 4),
                    ],
                }
                for t in shown
            ],
            "focus_track_id": names[focus.track_id],
            "_crop_policy": "candidate_zoom_v2",
        }
        from scripts.eval_slow_system import parse_model_decision

        started = time.perf_counter()
        try:
            raw, latency = self.slow(row)
        except Exception as exc:  # timeouts or server faults leave the track unverified
            return {
                "track_id": focus.track_id,
                "identity": None,
                "error": f"{type(exc).__name__}: {exc}",
                "latency_s": time.perf_counter() - started,
            }
        self.slow_calls += 1
        decision, error = parse_model_decision(raw)
        identity = identity_of(decision["decision"]) if decision else None
        return {
            "track_id": focus.track_id,
            "identity": identity,
            "decision": decision["decision"] if decision else None,
            "reason": decision["reason"] if decision else None,
            "error": error,
            "latency_s": float(latency),
        }

    def act(self, observation: dict[str, Any], *, pose_ned, yaw_deg: float):
        import numpy as np
        from PIL import Image

        step = self.loop.step_index
        rgb = np.asarray(Image.open(io.BytesIO(observation["rgb"])).convert("RGB"))
        frame_path = self.work_dir / f"frame_{step:04d}.png"
        frame_path.write_bytes(observation["rgb"])
        started = time.perf_counter()
        detections = self.proposer(rgb[:, :, ::-1].copy())
        fast_s = time.perf_counter() - started
        current = self.loop.update_tracks(detections)
        slow = []
        if self.loop.state in {"searching", "approaching"}:
            for track in self.loop.tracks_needing_verdict(current):
                verdict = self._slow_identity(frame_path, current, track)
                self.loop.apply_verdict(track.track_id, verdict["identity"])
                slow.append(verdict)
        camera_info = observation["camera_info"]

        def ground_point(u: float, v: float):
            # Full attitude + camera mount inverse of the projection model.
            return pixel_ground_point(
                u, v, pose=observation, camera_info=camera_info, image_size=self.image_size
            )

        action, info = self.loop.next_action(
            current, pose_ned=tuple(pose_ned), yaw_deg=yaw_deg, ground_point=ground_point
        )
        labels = {
            "onboard": {
                **info,
                "state": self.loop.state,
                "detections": len(detections),
                "tracks": [
                    {"id": t.track_id, "bbox": [round(v, 1) for v in t.bbox], "verdict": t.verdict}
                    for t in current
                ],
                "slow_calls": slow,
                "fast_latency_s": round(fast_s, 4),
                "confirmed": self.loop.confirmed,
            }
        }
        if not math.isfinite(sum(pose_ned)):
            raise RuntimeError("non-finite pose")
        return action, labels


def build_onboard_policy(
    *,
    task_text: str,
    world_path: Path | None,
    altitude_m: float,
    camera_info: dict[str, float],
    image_size: tuple[int, int],
    work_dir: Path,
    config_path: Path | None = None,
) -> OnboardPolicy:
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path else None
    return OnboardPolicy(
        task_text=task_text,
        world_path=world_path,
        altitude_m=altitude_m,
        camera_info=camera_info,
        image_size=image_size,
        work_dir=work_dir,
        config=config,
    )


__all__ = ["OnboardPolicy", "VOCABULARY", "YoloeProposer", "build_onboard_policy"]
