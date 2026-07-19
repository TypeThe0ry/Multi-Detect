from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

from multidetect.camera_calibration import CharucoBoardSpec, CompatibleCharucoDetector
from multidetect.vision import CameraReadError, CaptureConfig, OpenCVFrameSource


@dataclass(frozen=True, slots=True)
class BoardViewSignature:
    center_x: float
    center_y: float
    area_scale: float
    angle: float

    def distance(self, other: BoardViewSignature) -> float:
        return math.sqrt(
            (self.center_x - other.center_x) ** 2
            + (self.center_y - other.center_y) ** 2
            + (self.area_scale - other.area_scale) ** 2
            + (self.angle - other.angle) ** 2
        )


def board_view_signature(points: np.ndarray, *, width: int, height: int) -> BoardViewSignature:
    flattened = np.asarray(points, dtype=np.float32).reshape(-1, 2)
    if len(flattened) < 3 or width <= 0 or height <= 0:
        raise ValueError("board view signature requires image geometry and at least three points")
    hull = cv2.convexHull(flattened.reshape(-1, 1, 2))
    area_fraction = float(cv2.contourArea(hull) / float(width * height))
    rectangle = cv2.minAreaRect(hull)
    angle = float(rectangle[2])
    if rectangle[1][0] < rectangle[1][1]:
        angle += 90.0
    center = np.mean(flattened, axis=0)
    return BoardViewSignature(
        center_x=float(center[0] / width),
        center_y=float(center[1] / height),
        area_scale=math.sqrt(max(0.0, area_fraction)),
        angle=((angle + 90.0) % 180.0 - 90.0) / 90.0,
    )


def is_novel_view(
    signature: BoardViewSignature,
    accepted: list[BoardViewSignature],
    *,
    minimum_distance: float,
) -> bool:
    if not accepted:
        return True
    return min(signature.distance(previous) for previous in accepted) >= minimum_distance


def _atomic_json(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary_name).replace(path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _atomic_jpeg(path: Path, image: np.ndarray, quality: int = 94) -> None:
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("camera calibration frame encoding failed")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded.tobytes())
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary_name).replace(path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect only sharp, diverse ChArUco views from the real camera"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source")
    source.add_argument("--source-env")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--report-out", type=Path, required=True)
    parser.add_argument("--target-frames", type=int, default=30)
    parser.add_argument("--maximum-seconds", type=float, default=300.0)
    parser.add_argument("--sample-interval-seconds", type=float, default=0.15)
    parser.add_argument("--minimum-corners", type=int, default=12)
    parser.add_argument("--minimum-sharpness", type=float, default=45.0)
    parser.add_argument("--minimum-board-area-fraction", type=float, default=0.015)
    parser.add_argument("--minimum-novelty", type=float, default=0.055)
    parser.add_argument("--rtsp-transport", choices=("tcp", "udp"), default="tcp")
    parser.add_argument("--backend", choices=("auto", "ffmpeg", "gstreamer"), default="ffmpeg")
    parser.add_argument("--squares-x", type=int, default=7)
    parser.add_argument("--squares-y", type=int, default=5)
    parser.add_argument("--square-length-mm", type=float, default=40.0)
    parser.add_argument("--marker-length-mm", type=float, default=20.0)
    args = parser.parse_args()
    if args.target_frames < 20:
        parser.error("--target-frames must be at least 20")
    if args.maximum_seconds <= 0.0 or args.sample_interval_seconds < 0.0:
        parser.error("capture timing must be positive")
    if args.minimum_corners < 6:
        parser.error("--minimum-corners must be at least 6")
    if args.minimum_sharpness <= 0.0:
        parser.error("--minimum-sharpness must be positive")
    if not 0.0 < args.minimum_board_area_fraction < 1.0:
        parser.error("--minimum-board-area-fraction must be in (0, 1)")
    if not 0.0 < args.minimum_novelty < 1.0:
        parser.error("--minimum-novelty must be in (0, 1)")
    return args


def main() -> int:
    args = parse_args()
    source_value = args.source
    if args.source_env is not None:
        source_value = os.environ.get(args.source_env)
        if source_value is None or not source_value.strip():
            raise ValueError(f"camera source environment variable is empty: {args.source_env}")
    if source_value is None:
        raise ValueError("camera source is empty")
    if source_value.isdecimal():
        source_value = int(source_value)

    board_spec = CharucoBoardSpec(
        squares_x=args.squares_x,
        squares_y=args.squares_y,
        square_length_m=args.square_length_mm / 1000.0,
        marker_length_m=args.marker_length_mm / 1000.0,
    )
    detector = CompatibleCharucoDetector(board_spec)
    frame_source = OpenCVFrameSource(
        CaptureConfig(
            source=source_value,
            backend=args.backend,
            rtsp_transport=args.rtsp_transport,
            reconnect_attempts=2,
        )
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    accepted: list[BoardViewSignature] = []
    rejected = {"no_board": 0, "few_corners": 0, "blurred": 0, "too_small": 0, "duplicate": 0}
    read_errors = 0
    started_at = time.monotonic()
    next_sample_at = started_at
    try:
        while (
            len(accepted) < args.target_frames
            and time.monotonic() - started_at < args.maximum_seconds
        ):
            try:
                frame = frame_source.read()
            except CameraReadError:
                read_errors += 1
                time.sleep(min(0.5, args.sample_interval_seconds))
                continue
            now = time.monotonic()
            if now < next_sample_at:
                continue
            next_sample_at = now + args.sample_interval_seconds
            corners, ids, _marker_corners, _marker_ids = detector.detect_board(frame.image_bgr)
            if corners is None or ids is None:
                rejected["no_board"] += 1
                continue
            if len(ids) < args.minimum_corners:
                rejected["few_corners"] += 1
                continue
            grayscale = cv2.cvtColor(frame.image_bgr, cv2.COLOR_BGR2GRAY)
            sharpness = float(cv2.Laplacian(grayscale, cv2.CV_64F).var())
            if sharpness < args.minimum_sharpness:
                rejected["blurred"] += 1
                continue
            signature = board_view_signature(corners, width=frame.width, height=frame.height)
            if signature.area_scale**2 < args.minimum_board_area_fraction:
                rejected["too_small"] += 1
                continue
            if not is_novel_view(signature, accepted, minimum_distance=args.minimum_novelty):
                rejected["duplicate"] += 1
                continue
            accepted.append(signature)
            output = args.output_dir / f"charuco-{len(accepted):03d}.jpg"
            _atomic_jpeg(output, frame.image_bgr)
            print(
                json.dumps(
                    {
                        "event": "calibration_frame_accepted",
                        "accepted": len(accepted),
                        "target": args.target_frames,
                        "corners": len(ids),
                        "sharpness": round(sharpness, 2),
                        "board_area_fraction": round(signature.area_scale**2, 5),
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )
    finally:
        frame_source.close()

    elapsed = time.monotonic() - started_at
    report: dict[str, object] = {
        "schema_version": 1,
        "source_kind": (
            "rtsp"
            if isinstance(source_value, str) and source_value.startswith("rtsp://")
            else "local"
        ),
        "board": asdict(board_spec),
        "accepted_frames": len(accepted),
        "target_frames": args.target_frames,
        "complete": len(accepted) >= args.target_frames,
        "rejected": rejected,
        "read_errors": read_errors,
        "reconnect_count": frame_source.reconnect_count,
        "elapsed_seconds": round(elapsed, 3),
        "views": [asdict(item) for item in accepted],
    }
    _atomic_json(args.report_out, report)
    print(json.dumps(report, separators=(",", ":")), flush=True)
    return 0 if report["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
