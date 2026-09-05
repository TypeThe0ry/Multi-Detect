#!/usr/bin/env python3
"""Run truth-isolated Gazebo pixels-to-WGS84 software acceptance."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    repository_source = Path(__file__).resolve().parents[1] / "src"
    if repository_source.is_dir() and str(repository_source) not in sys.path:
        sys.path.insert(0, str(repository_source))

from multidetect.gazebo_target_geolocation_acceptance import (  # noqa: E402
    GazeboTargetGeolocationConfig,
    GazeboTargetGeolocationInputError,
    build_input_error_document,
    build_runtime_error_document,
    evaluate_gazebo_target_geolocation,
    write_evidence_exclusive,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Multi-Detect checkout containing simulation/gazebo",
    )
    parser.add_argument(
        "--frame-set-manifest",
        type=Path,
        required=True,
        help="immutable schema-v2 Gazebo BGR24 frame-set manifest",
    )
    parser.add_argument(
        "--scenario-manifest",
        type=Path,
        required=True,
        help="truth-separated scenario manifest index",
    )
    parser.add_argument("--model", type=Path, required=True, help="raw Ultralytics ONNX")
    parser.add_argument(
        "--model-manifest",
        type=Path,
        required=True,
        help="strict quarantined candidate model manifest",
    )
    parser.add_argument("--expected-label", default="car")
    parser.add_argument("--confidence-threshold", type=float, default=0.25)
    parser.add_argument("--nms-iou-threshold", type=float, default=0.45)
    parser.add_argument("--maximum-detections", type=int, default=300)
    parser.add_argument(
        "--provider",
        action="append",
        dest="providers",
        help="explicit ONNX Runtime provider; repeat to preserve requested order",
    )
    parser.add_argument("--minimum-anchor-sigma-px", type=float, default=2.0)
    parser.add_argument("--roll-sigma-deg", type=float, default=0.3)
    parser.add_argument("--pitch-sigma-deg", type=float, default=0.3)
    parser.add_argument("--heading-sigma-deg", type=float, default=1.0)
    parser.add_argument("--maximum-horizontal-error-m", type=float, default=8.0)
    parser.add_argument("--maximum-multiframe-stability-m", type=float, default=2.0)
    parser.add_argument("--minimum-one-sigma-coverage", type=float, default=0.60)
    parser.add_argument("--minimum-range-ci95-coverage", type=float, default=0.80)
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="new immutable machine-readable JSON evidence path",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    providers = tuple(args.providers or ("CPUExecutionProvider",))
    try:
        config = GazeboTargetGeolocationConfig(
            repository_root=args.repository_root,
            frame_set_manifest_path=args.frame_set_manifest,
            scenario_manifest_path=args.scenario_manifest,
            model_path=args.model,
            model_manifest_path=args.model_manifest,
            expected_label=args.expected_label,
            confidence_threshold=args.confidence_threshold,
            nms_iou_threshold=args.nms_iou_threshold,
            maximum_detections=args.maximum_detections,
            providers=providers,
            minimum_anchor_sigma_px=args.minimum_anchor_sigma_px,
            roll_sigma_deg=args.roll_sigma_deg,
            pitch_sigma_deg=args.pitch_sigma_deg,
            heading_sigma_deg=args.heading_sigma_deg,
            maximum_horizontal_error_m=args.maximum_horizontal_error_m,
            maximum_multiframe_stability_m=args.maximum_multiframe_stability_m,
            minimum_one_sigma_coverage=args.minimum_one_sigma_coverage,
            minimum_range_ci95_coverage=args.minimum_range_ci95_coverage,
        )
        document = evaluate_gazebo_target_geolocation(config)
    except GazeboTargetGeolocationInputError as exc:
        document = build_input_error_document(str(exc))
        write_evidence_exclusive(args.out, document)
        print(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
        return 1
    except Exception as exc:  # dependency/session/runtime failures remain fail-closed
        document = build_runtime_error_document(str(exc), error_type=type(exc).__name__)
        write_evidence_exclusive(args.out, document)
        print(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
        return 1

    write_evidence_exclusive(args.out, document)
    print(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
    return 0 if document["gate_passed"] else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
