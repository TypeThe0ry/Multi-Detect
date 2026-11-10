"""YOLOE class-agnostic proposal inference and optional tiled merging."""

from __future__ import annotations

import math
import os
from collections.abc import Sequence
from pathlib import Path

from multidetect.autonomy.onboard_loop import Detection

from .vocabulary import VOCABULARY


class YoloeProposer:
    """Class-agnostic proposals with optional full-frame plus 2x2 tiled inference.

    ``proposer_tiled`` defaults off in OnboardPolicy. Tiles overlap by 20% of
    their width/height; all five passes use the same imgsz and confidence.
    ``proposer_tile_overlap`` and ``proposer_merge_iou`` control crop overlap
    and the class-agnostic merge. The loop's box filter runs after merging.
    """

    def __init__(
        self,
        weights: Path,
        *,
        confidence: float,
        imgsz: int,
        device: str | int = 0,
        tiled: bool = False,
        tile_overlap: float = 0.2,
        merge_iou: float = 0.5,
    ) -> None:
        if not isinstance(tiled, bool):
            raise ValueError("proposer_tiled must be a boolean")
        if not 0 <= tile_overlap < 1 or not 0 < merge_iou <= 1:
            raise ValueError("tile overlap must be in [0, 1); merge IoU must be in (0, 1]")
        os.environ.setdefault("YOLO_OFFLINE", "true")
        os.environ.setdefault("YOLO_AUTOINSTALL", "false")
        if os.environ.get("MD_GIMBAL_INVOCATION") is not None:
            from .runtime_assets import configure_candidate_clip
            configure_candidate_clip()
        from ultralytics import YOLOE

        self.model = YOLOE(str(weights))
        self.model.to(device)
        prompts = list(VOCABULARY)
        # Encode prompts before the first fp16 predict fuses the model.
        self.model.set_classes(prompts, self.model.get_text_pe(prompts))
        self.confidence, self.imgsz = confidence, imgsz
        self.device, self.tiled = device, tiled
        self.tile_overlap, self.merge_iou = tile_overlap, merge_iou

    def _predict(self, bgr) -> list[Detection]:
        result = self.model.predict(
            bgr, imgsz=self.imgsz, conf=self.confidence, device=self.device, verbose=False
        )[0]
        boxes = result.boxes
        return [
            Detection(tuple(float(v) for v in box), float(score), self.model.names[int(cls)])
            for box, score, cls in zip(
                boxes.xyxy.tolist(), boxes.conf.tolist(), boxes.cls.tolist(), strict=True
            )
        ]

    def tile_proposals(self, bgr) -> list[Detection]:
        """Four overlapping crops, resized by YOLOE to imgsz, in full-frame pixels."""
        height, width = bgr.shape[:2]
        tile_w = min(width, math.ceil(width / (2 - self.tile_overlap)))
        tile_h = min(height, math.ceil(height / (2 - self.tile_overlap)))
        detections = []
        for y in dict.fromkeys((0, height - tile_h)):
            for x in dict.fromkeys((0, width - tile_w)):
                crop = bgr[y : y + tile_h, x : x + tile_w].copy()
                for detection in self._predict(crop):
                    x1, y1, x2, y2 = detection.bbox
                    box = (
                        max(0.0, min(float(width), x1 + x)),
                        max(0.0, min(float(height), y1 + y)),
                        max(0.0, min(float(width), x2 + x)),
                        max(0.0, min(float(height), y2 + y)),
                    )
                    if box[2] > box[0] and box[3] > box[1]:
                        detections.append(Detection(box, detection.score, detection.label))
        return detections

    def merge(self, detections: Sequence[Detection]) -> list[Detection]:
        """Class-agnostic greedy NMS; downstream loop retains its normal limits."""
        kept: list[Detection] = []
        for detection in sorted(detections, key=lambda d: d.score, reverse=True):
            a = detection.bbox
            for prior in kept:
                b = prior.bbox
                intersection = max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(
                    0.0, min(a[3], b[3]) - max(a[1], b[1])
                )
                union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
                if union > 0 and intersection / union > self.merge_iou:
                    break
            else:
                kept.append(detection)
        return kept

    def __call__(self, bgr) -> list[Detection]:
        detections = self._predict(bgr)
        if self.tiled:
            # Keep full-frame context and objects that cross tile boundaries.
            return self.merge([*detections, *self.tile_proposals(bgr)])
        return detections
