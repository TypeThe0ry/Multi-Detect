#!/usr/bin/env python3
"""用 YOLOE + Qwen3-VL 双教师给真实视频帧打伪标签。【需要 GPU，只能由训练/算力线程排期】

  python pseudo_label.py --source youtube_cc --classes person,car,fire_hydrant,traffic_cone \
      --yoloe artifacts/cache/yoloe/yoloe-11s-seg.pt --vlm /path/to/Qwen3-VL-8B-Instruct

输出（<root>/<source>/labels_v1/）：
  labels/<clip>/frame_x.txt      共识框，YOLO 格式
  uncertain/<clip>/frame_x.txt   单教师框（训练时当忽略区/再审）
  report.json                    各类共识率、单教师占比、空帧数
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import data_root, read_jsonl  # noqa: E402
from consensus import fuse, to_yolo_line  # noqa: E402

try:  # 复用项目已有的类名 -> 英文提示词映射
    from open_vocab_detector import prompt_for  # type: ignore
except Exception:  # pragma: no cover
    def prompt_for(name: str) -> str:
        return name.replace("_", " ")


class YoloeTeacher:
    def __init__(self, weights: str, classes: list[str], conf: float = 0.15, device: str = "cuda:0"):
        from ultralytics import YOLOE  # 延迟导入，CPU 测试不需要

        self.model = YOLOE(weights)
        self.classes = classes
        self.model.set_classes([prompt_for(c) for c in classes],
                               self.model.get_text_pe([prompt_for(c) for c in classes]))
        self.conf, self.device = conf, device

    def __call__(self, image: Path) -> list[dict]:
        r = self.model.predict(str(image), conf=self.conf, device=self.device, verbose=False)[0]
        h, w = r.orig_shape
        out = []
        for b, c, s in zip(r.boxes.xyxy.tolist(), r.boxes.cls.tolist(), r.boxes.conf.tolist()):
            out.append({"cls": self.classes[int(c)],
                        "box": [b[0] / w, b[1] / h, b[2] / w, b[3] / h], "score": float(s)})
        return out


class VlmTeacher:
    """Qwen3-VL 定位：模型输出 0-1000 相对坐标的 JSON 框，解析后归一化。"""

    PROMPT = ('Detect every instance of: {names}. Reply only with JSON: '
              '[{{"label": "<name>", "bbox_2d": [x1, y1, x2, y2]}}]. Use [] if none are visible.')

    def __init__(self, path: str, classes: list[str], device: str = "cuda:0"):
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor

        self.processor = AutoProcessor.from_pretrained(path)
        self.model = AutoModelForImageTextToText.from_pretrained(
            path, torch_dtype=torch.bfloat16, device_map=device)
        self.classes = classes
        self.by_prompt = {prompt_for(c).lower(): c for c in classes}

    def __call__(self, image: Path) -> list[dict]:
        from PIL import Image

        img = Image.open(image).convert("RGB")
        names = ", ".join(prompt_for(c) for c in self.classes)
        msgs = [{"role": "user", "content": [{"type": "image", "image": img},
                                             {"type": "text", "text": self.PROMPT.format(names=names)}]}]
        inputs = self.processor.apply_chat_template(
            msgs, add_generation_prompt=True, tokenize=True, return_dict=True,
            return_tensors="pt").to(self.model.device)
        ids = self.model.generate(**inputs, max_new_tokens=512, do_sample=False)
        text = self.processor.batch_decode(ids[:, inputs["input_ids"].shape[1]:],
                                           skip_special_tokens=True)[0]
        return parse_vlm_boxes(text, self.by_prompt)


def parse_vlm_boxes(text: str, by_prompt: dict[str, str]) -> list[dict]:
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        cls = by_prompt.get(str(it.get("label", "")).lower())
        box = it.get("bbox_2d")
        if cls is None or not isinstance(box, list) or len(box) != 4:
            continue
        x1, y1, x2, y2 = (min(max(float(v) / 1000.0, 0.0), 1.0) for v in box)
        if x2 > x1 and y2 > y1:
            out.append({"cls": cls, "box": [x1, y1, x2, y2], "score": 0.8})  # VLM 无置信度，给固定先验
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="youtube_cc")
    ap.add_argument("--classes", required=True)
    ap.add_argument("--yoloe")
    ap.add_argument("--vlm")
    ap.add_argument("--iou", type=float, default=0.5)
    ap.add_argument("--allow-negatives", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out-name", default="labels_v1")
    args = ap.parse_args()

    classes = args.classes.split(",")
    fast, slow = YoloeTeacher(args.yoloe, classes), VlmTeacher(args.vlm, classes)
    root = data_root() / args.source
    out = root / args.out_name
    rows = read_jsonl(root / "frames.jsonl")[: args.limit or None]
    stats = {c: {"agree": 0, "single": 0} for c in classes}
    empty = 0
    for r in rows:
        img = root / r["image"]
        stem = Path(r["image"]).with_suffix(".txt")
        stem = Path(*stem.parts[1:])  # 去掉 frames/ 前缀
        lab, unc = out / "labels" / stem, out / "uncertain" / stem
        if lab.exists():
            continue  # 断点续跑，不覆盖
        a, sf, ss = fuse(fast(img), slow(img), args.iou)
        for d in a:
            stats[d["cls"]]["agree"] += 1
        for d in sf + ss:
            stats[d["cls"]]["single"] += 1
        if not a and not sf and not ss:
            empty += 1
            if not args.allow_negatives:
                continue
        lab.parent.mkdir(parents=True, exist_ok=True)
        unc.parent.mkdir(parents=True, exist_ok=True)
        lab.write_text("\n".join(to_yolo_line(classes.index(d["cls"]), d["box"]) for d in a) + ("\n" if a else ""))
        unc.write_text("\n".join(to_yolo_line(classes.index(d["cls"]), d["box"]) for d in sf + ss))
    (out / "report.json").write_text(json.dumps({"frames": len(rows), "empty": empty, "per_class": stats},
                                                indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
