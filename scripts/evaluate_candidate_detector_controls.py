#!/usr/bin/env python3
"""Evaluate a quarantined raw YOLO candidate with three pixel controls."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    repository_source = Path(__file__).resolve().parents[1] / "src"
    if repository_source.is_dir() and str(repository_source) not in sys.path:
        sys.path.insert(0, str(repository_source))

from multidetect.candidate_detector_controls import (  # noqa: E402
    CandidateDetectorControlsConfig,
    CandidateDetectorControlsInputError,
    build_input_error_document,
    build_runtime_error_document,
    evaluate_candidate_detector_controls,
    write_evidence,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True, help="raw Ultralytics ONNX")
    parser.add_argument(
        "--manifest", type=Path, required=True, help="strict quarantined model manifest"
    )
    parser.add_argument("--frame", type=Path, required=True, help="present control frame")
    parser.add_argument(
        "--annotation", type=Path, required=True, help="independent frame annotation JSON"
    )
    parser.add_argument(
        "--frame-set-manifest",
        type=Path,
        help="optional hash-bound Gazebo BGR24 frame-set manifest",
    )
    parser.add_argument("--width", type=int, help="raw .bgr24 frame width")
    parser.add_argument("--height", type=int, help="raw .bgr24 frame height")
    parser.add_argument("--confidence-threshold", type=float, default=0.25)
    parser.add_argument("--positive-min-confidence", type=float, default=0.25)
    parser.add_argument("--positive-min-iou", type=float, default=0.50)
    parser.add_argument("--nms-iou-threshold", type=float, default=0.45)
    parser.add_argument("--maximum-detections", type=int, default=300)
    parser.add_argument(
        "--provider",
        action="append",
        dest="providers",
        help="explicit ONNX Runtime provider; repeat to preserve requested order",
    )
    parser.add_argument("--out", type=Path, required=True, help="machine-readable JSON output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    providers = tuple(args.providers or ("CPUExecutionProvider",))
    try:
        config = CandidateDetectorControlsConfig(
            model_path=args.model,
            model_manifest_path=args.manifest,
            frame_path=args.frame,
            annotation_path=args.annotation,
            frame_width=args.width,
            frame_height=args.height,
            frame_set_manifest_path=args.frame_set_manifest,
            confidence_threshold=args.confidence_threshold,
            positive_min_confidence=args.positive_min_confidence,
            positive_min_iou=args.positive_min_iou,
            nms_iou_threshold=args.nms_iou_threshold,
            maximum_detections=args.maximum_detections,
            providers=providers,
        )
        document = evaluate_candidate_detector_controls(config)
    except CandidateDetectorControlsInputError as exc:
        document = build_input_error_document(str(exc))
        write_evidence(args.out, document)
        print(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
        return 1
    except Exception as exc:  # dependency/session/runtime failures are fail-closed evidence
        document = build_runtime_error_document(str(exc), error_type=type(exc).__name__)
        write_evidence(args.out, document)
        print(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
        return 1

    write_evidence(args.out, document)
    print(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
    return 0 if document["gate_passed"] else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
