from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from multidetect.vision import CaptureConfig, OnnxNx6Config, OnnxNx6Detector, OpenCVFrameSource


def _source(value: str) -> int | str:
    return int(value) if value.isdecimal() else value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _capture_source(args: argparse.Namespace) -> int | str:
    if args.source_env is not None:
        value = os.environ.get(args.source_env)
        if value is None:
            raise ValueError(f"camera source environment variable is missing: {args.source_env}")
        if not value.strip():
            raise ValueError("camera source environment variable is empty")
        return _source(value.strip())
    return _source(args.source if args.source is not None else "0")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture a confirmed no-fire camera session for hard-negative mining."
    )
    parser.add_argument("--onnx-model", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--source")
    source.add_argument(
        "--source-env",
        metavar="ENV_VAR",
        help="read RTSP/local source from an environment variable so credentials stay out of argv",
    )
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--scene-notes", default="")
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--fps", type=float)
    parser.add_argument("--rtsp-transport", choices=("tcp", "udp"), default="tcp")
    parser.add_argument(
        "--backend",
        choices=("auto", "dshow", "msmf", "ffmpeg"),
        default="auto",
    )
    parser.add_argument("--reconnect-attempts", type=int, default=10)
    parser.add_argument("--reconnect-delay-seconds", type=float, default=0.25)
    parser.add_argument("--provider", default="CUDAExecutionProvider")
    parser.add_argument("--frames", type=int, default=1800)
    parser.add_argument("--confidence", type=float, default=0.05)
    parser.add_argument("--sample-every", type=int, default=60)
    parser.add_argument("--trigger-spacing", type=int, default=8)
    parser.add_argument("--max-saved", type=int, default=600)
    parser.add_argument("--confirm-no-fire", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.confirm_no_fire:
        raise ValueError("--confirm-no-fire is required before creating empty labels")
    if not args.onnx_model.is_file():
        raise FileNotFoundError(args.onnx_model)
    if not args.session_id.strip():
        raise ValueError("--session-id cannot be empty")
    if args.frames <= 0 or args.sample_every <= 0 or args.trigger_spacing <= 0:
        raise ValueError("frame and spacing values must be positive")
    if args.max_saved <= 0 or not 0.0 <= args.confidence <= 1.0:
        raise ValueError("max-saved/confidence values are invalid")

    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("opencv-python is required for camera mining") from exc

    if args.out.exists() and any(args.out.rglob("*")):
        raise FileExistsError("hard-negative output directory must be new or empty")
    image_dir = args.out / "images"
    label_dir = args.out / "labels"
    image_dir.mkdir(parents=True, exist_ok=True)
    label_dir.mkdir(parents=True, exist_ok=True)
    detector = OnnxNx6Detector(
        OnnxNx6Config(
            model_path=args.onnx_model.resolve(),
            class_names=("flame", "smoke"),
            confidence_threshold=args.confidence,
            output_coordinates="letterbox_xyxy_px",
            providers=(args.provider, "CPUExecutionProvider"),
        )
    )
    capture_config = CaptureConfig(
        source=_capture_source(args),
        width=args.width,
        height=args.height,
        fps=args.fps,
        rtsp_transport=args.rtsp_transport,
        backend=args.backend,
        reconnect_attempts=args.reconnect_attempts,
        reconnect_delay_seconds=args.reconnect_delay_seconds,
    )
    source = OpenCVFrameSource(capture_config)
    saved = 0
    triggered_saved = 0
    periodic_saved = 0
    detection_count = 0
    maximum_confidence = 0.0
    last_triggered_frame = -args.trigger_spacing
    metadata_path = args.out / "metadata.jsonl"
    observed_sizes: set[tuple[int, int]] = set()
    with source, metadata_path.open("w", encoding="utf-8", newline="\n") as metadata:
        for frame_index in range(args.frames):
            captured = source.read()
            observed_sizes.add((captured.width, captured.height))
            detections = detector.detect(captured.image_bgr)
            detection_count += len(detections)
            if detections:
                maximum_confidence = max(
                    maximum_confidence,
                    max(detection.confidence for detection in detections),
                )
            triggered = bool(detections) and (
                frame_index - last_triggered_frame >= args.trigger_spacing
            )
            periodic = frame_index % args.sample_every == 0
            if not (triggered or periodic) or saved >= args.max_saved:
                continue
            stem = f"camera-negative-{frame_index:06d}"
            image_path = image_dir / f"{stem}.jpg"
            label_path = label_dir / f"{stem}.txt"
            if not cv2.imwrite(str(image_path), captured.image_bgr):
                raise RuntimeError(f"failed to write {image_path}")
            label_path.touch()
            metadata.write(
                json.dumps(
                    {
                        "frame_index": frame_index,
                        "frame_id": captured.frame_id,
                        "captured_at_s": captured.captured_at_s,
                        "triggered": triggered,
                        "periodic": periodic,
                        "detections": [
                            {
                                "label": detection.label,
                                "confidence": detection.confidence,
                                "bbox": detection.bbox.rounded(),
                            }
                            for detection in detections
                        ],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            saved += 1
            if triggered:
                triggered_saved += 1
                last_triggered_frame = frame_index
            else:
                periodic_saved += 1

    manifest = {
        "schema_version": 1,
        "event": "camera_hard_negative_session_recorded",
        "session_id": args.session_id,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "scene_notes": args.scene_notes,
        "source_kind": "rtsp" if capture_config.is_rtsp else "local_or_file",
        "source_description": capture_config.redacted_source_description,
        "source_uri_recorded": False,
        "operator_confirmed_no_fire": True,
        "processed_frames": args.frames,
        "saved_images": saved,
        "triggered_saved_images": triggered_saved,
        "periodic_saved_images": periodic_saved,
        "raw_detection_count": detection_count,
        "maximum_confidence": maximum_confidence,
        "sample_every": args.sample_every,
        "trigger_spacing": args.trigger_spacing,
        "candidate_confidence": args.confidence,
        "observed_resolutions": [list(size) for size in sorted(observed_sizes)],
        "camera_reconnect_count": source.reconnect_count,
        "model_path": str(args.onnx_model.resolve()),
        "model_sha256": _sha256(args.onnx_model),
        "providers": list(detector.provider_names),
        "labels_are_empty": True,
        "images_require_manual_review_before_training": True,
    }
    (args.out / "session-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                **manifest,
                "output_directory": str(args.out.resolve()),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
