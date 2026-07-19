from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .vision import _require_cv2


@dataclass(frozen=True, slots=True)
class VideoEvidenceProbe:
    path: Path
    decoded_frame_count: int
    declared_frame_count: int | None
    fps: float | None
    width: int | None
    height: int | None
    duration_s: float | None
    full_frame_scan_completed: bool
    stable_dimensions: bool
    passed: bool
    failure_reasons: tuple[str, ...]


def probe_video_evidence(
    path: str | Path,
    *,
    cv2_module: Any | None = None,
) -> VideoEvidenceProbe:
    """Decode every video frame and return conservative, offline evidence metadata."""

    path = Path(path)
    if not path.is_file():
        raise ValueError(f"tracking source video does not exist: {path}")
    cv2 = cv2_module if cv2_module is not None else _require_cv2()
    capture = cv2.VideoCapture(str(path), cv2.CAP_ANY)
    if not capture.isOpened():
        capture.release()
        return _failed_probe(path, "source video could not be opened by OpenCV")

    declared_raw = float(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    fps_raw = float(capture.get(cv2.CAP_PROP_FPS))
    declared_frame_count = (
        int(round(declared_raw)) if math.isfinite(declared_raw) and declared_raw > 0 else None
    )
    fps = fps_raw if math.isfinite(fps_raw) and fps_raw > 0.0 else None
    decoded_frame_count = 0
    width: int | None = None
    height: int | None = None
    stable_dimensions = True
    read_failed_with_declared_frames_remaining = False
    try:
        while True:
            ok, image = capture.read()
            if not ok or image is None:
                if declared_frame_count is not None and decoded_frame_count < declared_frame_count:
                    read_failed_with_declared_frames_remaining = True
                break
            shape = getattr(image, "shape", None)
            if not isinstance(shape, tuple) or len(shape) < 2:
                stable_dimensions = False
                break
            frame_height = int(shape[0])
            frame_width = int(shape[1])
            if frame_width <= 0 or frame_height <= 0:
                stable_dimensions = False
                break
            if width is None:
                width, height = frame_width, frame_height
            elif width != frame_width or height != frame_height:
                stable_dimensions = False
            decoded_frame_count += 1
    finally:
        capture.release()

    failures: list[str] = []
    if decoded_frame_count < 2:
        failures.append("source video must contain at least two decodable frames")
    if fps is None:
        failures.append("source video FPS metadata is missing or invalid")
    if width is None or height is None or not stable_dimensions:
        failures.append("source video dimensions are missing, invalid, or unstable")
    declared_tolerance = (
        max(2, math.ceil(declared_frame_count * 0.01)) if declared_frame_count is not None else 0
    )
    if (
        read_failed_with_declared_frames_remaining
        and declared_frame_count is not None
        and decoded_frame_count + declared_tolerance < declared_frame_count
    ):
        failures.append("source video ended before its declared frame count")
    duration_s = decoded_frame_count / fps if fps is not None else None
    return VideoEvidenceProbe(
        path=path,
        decoded_frame_count=decoded_frame_count,
        declared_frame_count=declared_frame_count,
        fps=fps,
        width=width,
        height=height,
        duration_s=duration_s,
        full_frame_scan_completed=not failures,
        stable_dimensions=stable_dimensions,
        passed=not failures,
        failure_reasons=tuple(failures),
    )


def video_evidence_document(probe: VideoEvidenceProbe) -> dict[str, object]:
    return {
        "decoded_frame_count": probe.decoded_frame_count,
        "declared_frame_count": probe.declared_frame_count,
        "fps": probe.fps,
        "width": probe.width,
        "height": probe.height,
        "duration_s": probe.duration_s,
        "full_frame_scan_completed": probe.full_frame_scan_completed,
        "stable_dimensions": probe.stable_dimensions,
        "passed": probe.passed,
        "failure_reasons": list(probe.failure_reasons),
    }


def _failed_probe(path: Path, reason: str) -> VideoEvidenceProbe:
    return VideoEvidenceProbe(
        path=path,
        decoded_frame_count=0,
        declared_frame_count=None,
        fps=None,
        width=None,
        height=None,
        duration_s=None,
        full_frame_scan_completed=False,
        stable_dimensions=False,
        passed=False,
        failure_reasons=(reason,),
    )


__all__ = ["VideoEvidenceProbe", "probe_video_evidence", "video_evidence_document"]
