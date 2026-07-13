from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate an Ultralytics checkpoint and save machine-readable metrics."
    )
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--split", default="test")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--device", default="0")
    parser.add_argument("--batch", type=int, default=32)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    try:
        from ultralytics import YOLO
    except ImportError as exc:
        raise RuntimeError("Ultralytics is required for checkpoint evaluation") from exc

    model = YOLO(str(args.model))
    metrics = model.val(
        data=str(args.data.resolve()),
        split=args.split,
        device=args.device,
        batch=args.batch,
        imgsz=args.imgsz,
        workers=args.workers,
        plots=False,
        verbose=False,
    )
    names = metrics.names
    per_class = []
    for index in range(len(metrics.box.p)):
        class_id = int(metrics.box.ap_class_index[index])
        per_class.append(
            {
                "class_id": class_id,
                "label": str(names[class_id]),
                "precision": float(metrics.box.p[index]),
                "recall": float(metrics.box.r[index]),
                "map50": float(metrics.box.ap50[index]),
                "map50_95": float(metrics.box.ap[index]),
            }
        )
    document: dict[str, Any] = {
        "event": "yolo_candidate_evaluated",
        "model": str(args.model.resolve()),
        "data": str(args.data.resolve()),
        "split": args.split,
        "summary": {
            "precision": float(metrics.box.mp),
            "recall": float(metrics.box.mr),
            "map50": float(metrics.box.map50),
            "map50_95": float(metrics.box.map),
        },
        "per_class": per_class,
        "speed_ms_per_image": {
            key: float(value) for key, value in metrics.speed.items() if value is not None
        },
        "production_approved": False,
        "physical_release_enabled": False,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
