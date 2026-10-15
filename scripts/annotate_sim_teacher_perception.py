"""Run production ONNX detectors over persisted SIM teacher episodes.

The annotator consumes only RGB/depth sensor payloads and camera metadata.  The
teacher labels in ``steps.jsonl`` are never loaded into the detector bridge.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.policy_v2.perception_bridge import (  # noqa: E402
    COCO80_CLASS_NAMES,
    DetectorSpec,
    PerceptionBridge,
    build_detector,
)
from multidetect.sim_teacher.recording import load_depth_array  # noqa: E402

DEFAULT_COCO_MODEL = ROOT / "artifacts/models/coco-yolo26n-traditional/yolo26n-traditional.onnx"
DEFAULT_RED_MODEL = (
    ROOT / "artifacts/models/red-car-detector-v2/yolo26n-red-car-v2/weights/best.onnx"
)

def _sensor_path(episode: Path, value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("observation sensor path is missing")
    # Captures were recorded on Windows and use backslashes even when consumed
    # from a POSIX worker.  Normalize both separators before joining.
    relative = Path(value.replace("\\", os.sep).replace("/", os.sep))
    return relative if relative.is_absolute() else episode / relative


def _episode_dirs(dataset: Path) -> list[Path]:
    return sorted({path.parent for path in dataset.rglob("steps.jsonl")})


def annotate_episode(episode: Path, bridge: PerceptionBridge, *, overwrite: bool = True) -> int:
    steps_path = episode / "steps.jsonl"
    output_path = episode / "perception.jsonl"
    if output_path.exists() and not overwrite:
        raise FileExistsError(output_path)
    rows: list[str] = []
    with steps_path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(f"{steps_path}:{line_number}: step must be an object")
            step_index = record.get("step_index", line_number - 1)
            for observation_name in ("before", "after"):
                observation = record.get(f"obs_{observation_name}")
                if not isinstance(observation, dict):
                    raise ValueError(
                        f"{steps_path}:{line_number}: obs_{observation_name} must be an object"
                    )
                rgb_path = _sensor_path(episode, observation.get("rgb_path"))
                image = cv2.imread(str(rgb_path), cv2.IMREAD_COLOR)
                if image is None:
                    raise FileNotFoundError(rgb_path)
                depth = None
                raw_depth = observation.get("depth_path")
                if raw_depth:
                    depth_path = _sensor_path(episode, raw_depth)
                    depth = load_depth_array(depth_path)
                else:
                    depth_path = None
                output = bridge.annotate_observation(
                    observation,
                    image,
                    depth,
                    rgb_path=str(rgb_path.relative_to(episode)),
                    depth_path=(str(depth_path.relative_to(episode)) if depth_path else None),
                )
                output.update({"step_index": step_index, "observation": observation_name})
                rows.append(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    output_path.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8", newline="\n")
    return len(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path, help="dataset root or one episode directory")
    parser.add_argument("--coco-model", type=Path, default=DEFAULT_COCO_MODEL)
    parser.add_argument("--red-car-model", type=Path, default=DEFAULT_RED_MODEL)
    parser.add_argument("--no-coco", action="store_true")
    parser.add_argument("--no-red-car", action="store_true")
    parser.add_argument("--confidence", type=float, default=0.25)
    parser.add_argument("--provider", action="append", default=[])
    parser.add_argument("--no-overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 0.0 <= args.confidence <= 1.0:
        raise ValueError("--confidence must be in [0, 1]")
    specs: list[DetectorSpec] = []
    if not args.no_coco:
        specs.append(
            DetectorSpec(
                args.coco_model,
                "coco-yolo26n-traditional",
                COCO80_CLASS_NAMES,
                "raw",
                args.confidence,
                tuple(args.provider),
            )
        )
    if not args.no_red_car:
        specs.append(
            DetectorSpec(
                args.red_car_model,
                "red-car-detector-v2",
                COCO80_CLASS_NAMES,
                "raw",
                args.confidence,
                tuple(args.provider),
            )
        )
    if not specs:
        raise ValueError("at least one detector must be enabled")
    for spec in specs:
        if not spec.model_path.is_file():
            raise FileNotFoundError(spec.model_path)
    bridge = PerceptionBridge(tuple((spec, build_detector(spec)) for spec in specs))
    dataset = args.dataset
    episodes = [dataset] if (dataset / "steps.jsonl").is_file() else _episode_dirs(dataset)
    if not episodes:
        raise ValueError(f"no episode steps.jsonl found under {dataset}")
    total = 0
    for episode in episodes:
        count = annotate_episode(episode, bridge, overwrite=not args.no_overwrite)
        total += count
        print(json.dumps({"episode": str(episode), "observations": count}, separators=(",", ":")))
    print(json.dumps({"episodes": len(episodes), "observations": total}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

