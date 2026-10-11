#!/usr/bin/env python3
"""Evaluate shadow grounding on aligned recorded SIM teacher frames.

The truth path is used only by this offline evaluator.  Runtime adapters never
receive these labels.  The default run is CPU-only HSV plus deterministic task
parsing; ``--vlm`` enables an explicitly local Qwen3-VL probe when the existing
snapshot and CUDA environment are available.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.policy_v2.vlm_grounding import (  # noqa: E402
    AttributeGrounder,
    TaskSpecResolver,
)

try:
    from scripts.autolabel_sim_teacher_frames import project_target_bbox
    from scripts.build_sim_detector_dataset import CLASS_ALIASES, DEFAULT_SIZES
except ModuleNotFoundError:  # pragma: no cover - direct script invocation fallback
    from autolabel_sim_teacher_frames import project_target_bbox
    from build_sim_detector_dataset import CLASS_ALIASES, DEFAULT_SIZES


COLORS = {"red", "blue", "green", "yellow", "orange", "black", "white"}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def world_id(episode: Path) -> str:
    for part in episode.parts:
        if part.startswith("gen_w"):
            return part
    return ""


def truth_for(world: str) -> dict[str, Any] | None:
    path = ROOT / "configs/scenarios/generated" / f"{world}.truth.json"
    return load_json(path) if path.is_file() else None


def canonical_color(target: dict[str, Any]) -> str | None:
    label = str(target.get("label", "")).casefold()
    for color in COLORS:
        if label.startswith(color + "_"):
            return color
    value = str(target.get("color", "")).casefold()
    aliases = {"red": "red", "blue": "blue", "green": "green", "yellow": "yellow", "orange": "orange"}
    return aliases.get(value)


def aligned_rows(roots: list[Path], *, max_rows: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for root in roots:
        for episode in sorted(root.rglob("steps.jsonl")):
            episode = episode.parent
            metadata = episode / "episode.json"
            if not metadata.is_file() or load_json(metadata).get("status") != "complete":
                continue
            world = world_id(episode)
            if not world:
                continue  # urban and other non-aligned campaigns are excluded
            truth = truth_for(world)
            if not truth or not isinstance(truth.get("targets"), list):
                continue
            for line in (episode / "steps.jsonl").read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                step = json.loads(line)
                if any(abs(float(step.get(f"obs_{side}", {}).get("sim_skew_ms", 0.0))) > 250.0 for side in ("before", "after")):
                    continue
                for side in ("before", "after"):
                    obs = step.get("obs_" + side)
                    if not isinstance(obs, dict) or not isinstance(obs.get("rgb_path"), str):
                        continue
                    rgb_path = episode / Path(obs["rgb_path"].replace("\\", os.sep).replace("/", os.sep))
                    bgr = cv2.imread(str(rgb_path), cv2.IMREAD_COLOR)
                    if bgr is None:
                        continue
                    height, width = bgr.shape[:2]
                    objects: list[dict[str, Any]] = []
                    pose = {**(obs.get("pose") or {}), "attitude": obs.get("attitude") or {}}
                    for target in truth["targets"]:
                        if not isinstance(target, dict):
                            continue
                        metric_pose = target.get("pose") or {}
                        try:
                            metric = (float(metric_pose["y_north_m"]), float(metric_pose["x_east_m"]), float(metric_pose["z_up_m"]))
                            name = CLASS_ALIASES.get(str(target.get("class") or target.get("label") or "vehicle"), str(target.get("class") or "vehicle"))
                            size = tuple(target.get("size_m") or DEFAULT_SIZES.get(name, (3.0, 1.8, 1.5)))
                            box = project_target_bbox(target_ned=metric, pose=pose, camera_info=obs.get("camera_info", {}), image_size=(width, height), target_size_m=size, target_yaw_rad=float(metric_pose.get("yaw_rad", 0.0)))
                        except (KeyError, TypeError, ValueError):
                            continue
                        if box is None or box[2] - box[0] < 4.0 or box[3] - box[1] < 4.0:
                            continue
                        objects.append({"box": box, "class": name, "color": canonical_color(target), "target_id": target.get("target_id")})
                    if objects:
                        rows.append({"rgb": cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), "path": str(rgb_path), "objects": objects, "world": world, "episode": episode.name})
                        if max_rows and len(rows) >= max_rows:
                            return rows
    return rows


def evaluate_hsv(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grounder = AttributeGrounder()
    color_correct = color_total = class_correct = class_total = color_unknown = class_unknown = 0
    by_color: dict[str, list[int]] = {}
    by_class: dict[str, list[int]] = {}
    for row in rows:
        for obj in row["objects"]:
            result = grounder.ground(row["rgb"], [obj["box"]], target_color=obj["color"])[0]
            if obj["color"]:
                color_total += 1
                hit = result.observed_color == obj["color"]
                color_correct += int(hit)
                color_unknown += int(result.observed_color is None)
                by_color.setdefault(obj["color"], [0, 0])[0] += int(hit)
                by_color[obj["color"]][1] += 1
            # HSV intentionally does not claim class identity.
            class_total += 1
            class_unknown += 1
            by_class.setdefault(obj["class"], [0, 0])[1] += 1
    return {"frames": len(rows), "boxes": class_total, "color_accuracy": color_correct / color_total if color_total else None, "class_accuracy": class_correct / class_total if class_total else None, "color_count": color_total, "class_count": class_total, "color_unknown": color_unknown, "class_unknown": class_unknown, "color_unknown_rate": color_unknown / color_total if color_total else None, "class_unknown_rate": class_unknown / class_total if class_total else None, "color_by_label": by_color, "class_by_label": by_class}


class LocalQwenGrounder:
    """Tiny optional local adapter; no network access and no flight authority."""

    def __init__(self, snapshot: Path, *, max_new_tokens: int = 96) -> None:
        import torch
        from transformers import AutoProcessor, Qwen3VLForConditionalGeneration

        self.torch = torch
        started = time.perf_counter()
        self.processor = AutoProcessor.from_pretrained(str(snapshot), local_files_only=True, trust_remote_code=False)
        self.model = Qwen3VLForConditionalGeneration.from_pretrained(str(snapshot), local_files_only=True, trust_remote_code=False, torch_dtype=torch.bfloat16, device_map={"": "cuda:0"}, attn_implementation="sdpa").eval()
        torch.cuda.synchronize()
        self.load_s = time.perf_counter() - started
        self.max_new_tokens = max_new_tokens
        self.latencies: list[float] = []

    def ground_attributes(self, crop: np.ndarray, prompt: str) -> str:
        from PIL import Image

        image = Image.fromarray(np.asarray(crop, dtype=np.uint8), mode="RGB")
        messages = [{"role": "user", "content": [{"type": "image", "image": image}, {"type": "text", "text": "Return ONLY strict JSON with keys class_match,color_match,attribute_match,score,observed_class,observed_color. Values match true/false/unknown. " + prompt}]}]
        started = time.perf_counter()
        inputs = self.processor.apply_chat_template(messages, tokenize=True, add_generation_prompt=True, return_dict=True, return_tensors="pt")
        input_tokens = int(inputs["input_ids"].shape[-1])
        inputs = inputs.to("cuda:0")
        with self.torch.inference_mode():
            generated = self.model.generate(**inputs, max_new_tokens=self.max_new_tokens, do_sample=False, use_cache=True)
        self.torch.cuda.synchronize()
        raw = self.processor.tokenizer.decode(generated[0, input_tokens:].detach().cpu().tolist(), skip_special_tokens=True, clean_up_tokenization_spaces=False)
        self.latencies.append(time.perf_counter() - started)
        return raw


def evaluate_vlm(rows: list[dict[str, Any]], snapshot: Path, limit: int) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        backend = LocalQwenGrounder(snapshot)
    except Exception as exc:
        return {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
    grounder = AttributeGrounder(backend, timeout_s=30.0)
    color_correct = class_correct = color_total = class_total = 0
    for row in rows:
        for obj in row["objects"]:
            if color_total >= limit:
                break
            result = grounder.ground(row["rgb"], [obj["box"]], target_class=obj["class"], target_color=obj["color"])[0]
            if obj["color"]:
                color_total += 1
                color_correct += int(result.observed_color == obj["color"])
            class_total += 1
            class_correct += int(result.observed_class == obj["class"])
        if color_total >= limit:
            break
    latencies = backend.latencies
    return {"status": "ok", "samples": color_total, "color_accuracy": color_correct / color_total if color_total else None, "class_accuracy": class_correct / class_total if class_total else None, "color_count": color_total, "class_count": class_total, "load_s": backend.load_s, "wall_s": time.perf_counter() - started, "p50_s": statistics.median(latencies) if latencies else None, "p95_s": float(np.percentile(latencies, 95)) if latencies else None, "peak_allocated_bytes": int(backend.torch.cuda.max_memory_allocated())}


PROMPTS = (
    ("帮我找到最近的红车", "vehicle", "red", "nearest"), ("搜索前方一公里的蓝色货车", "cargo_cart", "blue", "any"),
    ("find the closest green rover", "rover", "green", "nearest"), ("look for a yellow car within 500m", "vehicle", "yellow", "any"),
    ("找到橙色车辆", "vehicle", "orange", "any"), ("找最近的蓝色探测车", "rover", "blue", "nearest"),
    ("search for a red truck", "cargo_cart", "red", "any"), ("搜索绿色卡车两公里", "cargo_cart", "green", "any"),
    ("找到黄色漫游车", "rover", "yellow", "any"), ("find a blue vehicle", "vehicle", "blue", "any"),
    ("最近的绿色汽车", "vehicle", "green", "nearest"), ("search nearest red rover", "rover", "red", "nearest"),
    ("找一辆白车", "vehicle", "white", "any"), ("find the nearest black car", "vehicle", "black", "nearest"),
    ("搜索前方三百米的红色车辆", "vehicle", "red", "any"), ("look for the closest orange truck", "cargo_cart", "orange", "nearest"),
    ("找蓝色货车并记录位置", "cargo_cart", "blue", "any"), ("find a green rover with a beacon", "rover", "green", "any"),
    ("搜索黄色车", "vehicle", "yellow", "any"), ("帮我找最近的红色卡车", "cargo_cart", "red", "nearest"),
    ("find any blue rover", "rover", "blue", "any"), ("找绿色车辆", "vehicle", "green", "any"),
    ("search for a red car within 1 km", "vehicle", "red", "any"), ("最近的黄车", "vehicle", "yellow", "nearest"),
    ("找一辆车", None, None, "any"), ("find the target", None, None, "any"),
    ("搜索最近的目标", None, None, "nearest"), ("帮我看看前面有什么", None, None, "any"),
    ("找红色或蓝色的车", None, None, "any"), ("find a car or a truck", None, None, "any"),
    ("颜色不确定的车辆", "vehicle", None, "any"), ("search an unknown object", None, None, "any"),
    ("最近的红车或蓝车", None, None, "nearest"), ("look for the closest vehicle", "vehicle", None, "nearest"),
)


def evaluate_tasks() -> dict[str, Any]:
    resolver = TaskSpecResolver(vlm=None, dev_fallback=True)
    correct = 0
    rows = []
    for text, expected_class, expected_color, expected_relative in PROMPTS:
        spec = resolver.resolve(text)
        ok = (spec.target_class == expected_class and spec.target_color == expected_color and spec.relative == expected_relative)
        correct += int(ok)
        rows.append({"prompt": text, "expected": [expected_class, expected_color, expected_relative], "actual": [spec.target_class, spec.target_color, spec.relative], "unknown_fields": list(spec.unknown_fields), "correct": ok})
    return {"count": len(rows), "correct": correct, "accuracy": correct / len(rows), "rows": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", action="append", type=Path, default=None)
    parser.add_argument("--max-rows", type=int, default=None)
    parser.add_argument("--vlm", action="store_true", help="run optional local Qwen probe")
    parser.add_argument("--vlm-limit", type=int, default=16)
    parser.add_argument("--snapshot", type=Path, default=ROOT / "artifacts/cache/huggingface-vlm/models--Qwen--Qwen3-VL-2B-Instruct/snapshots/89644892e4d85e24eaac8bacfd4f463576704203")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    roots = args.root or [ROOT / "artifacts/datasets/sim-teacher/campaign-20261011-r2", ROOT / "artifacts/datasets/sim-teacher/campaign-20261011-r3"]
    rows = aligned_rows(roots, max_rows=args.max_rows)
    result = {"contract": "multidetect.vlm-grounding-evaluation-v1", "roots": [str(path) for path in roots], "aligned_frames": len(rows), "hsv": evaluate_hsv(rows), "task_spec": evaluate_tasks()}
    if args.vlm:
        result["vlm"] = evaluate_vlm(rows, args.snapshot, max(0, args.vlm_limit))
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8", newline="\n")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
