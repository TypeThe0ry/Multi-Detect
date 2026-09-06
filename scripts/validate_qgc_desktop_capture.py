"""Validate exact-client desktop captures of QGC's native video surface.

The combined Windows acceptance wrapper already proves that the real QGC
process connected to RTSP, decoded frames, and emitted signed post-adoption
type-24 acknowledgements.  This validator supplies the missing composition
evidence: two exact-client desktop captures must contain a non-black,
non-uniform video region while the native acknowledgement sequence advances.

The Gazebo acceptance scene is intentionally almost static, so temporal pixel
change is recorded as useful diagnostic evidence rather than treated as a hard
gate.  The result never claims exact source-frame identity, complete QML
semantics, or human operator acceptance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

SCHEMA = "multidetect.qgc-desktop-live-video-proof"
SCHEMA_VERSION = 1

# These regions are wholly inside the full-state QGC video surface in the
# native Fly View.  They deliberately avoid the native toolbar, map PiP,
# centre crosshair, runtime badge, action chrome, and depth/status panels.
# Coordinates are normalized exact-client coordinates: x0, y0, x1, y1.
DEFAULT_VIDEO_REGIONS: tuple[tuple[float, float, float, float], ...] = (
    (0.58, 0.22, 0.92, 0.44),
    (0.60, 0.54, 0.92, 0.78),
)


@dataclass(frozen=True)
class SurfaceThresholds:
    maximum_black_pixel_ratio: float = 0.95
    minimum_mean_luminance: float = 12.0
    minimum_luminance_range: float = 2.0
    minimum_luminance_stddev: float = 0.75
    minimum_unique_sampled_colors: int = 4
    black_luminance_ceiling: int = 8
    temporal_delta_threshold: int = 8
    temporal_changed_pixel_ratio: float = 0.001
    temporal_mean_absolute_difference: float = 0.10
    temporal_dhash_distance: int = 2


DEFAULT_THRESHOLDS = SurfaceThresholds()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _load_capture(path: Path) -> np.ndarray:
    if not path.is_file():
        raise ValueError(f"QGC desktop capture is missing: {path}")
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError(f"OpenCV could not decode a BGR capture: {path}")
    return image


def _normalized_regions(
    regions: Sequence[Sequence[float]],
) -> tuple[tuple[float, float, float, float], ...]:
    normalized: list[tuple[float, float, float, float]] = []
    for index, raw in enumerate(regions):
        if len(raw) != 4:
            raise ValueError(f"video region {index} must contain x0,y0,x1,y1")
        x0, y0, x1, y1 = (float(value) for value in raw)
        if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
            raise ValueError(f"video region {index} escapes normalized client coordinates")
        normalized.append((x0, y0, x1, y1))
    if not normalized:
        raise ValueError("at least one video-only region is required")
    return tuple(normalized)


def _mask_for_regions(
    height: int,
    width: int,
    regions: Sequence[Sequence[float]],
) -> tuple[np.ndarray, list[dict[str, Any]]]:
    mask = np.zeros((height, width), dtype=np.uint8)
    resolved: list[dict[str, Any]] = []
    for x0, y0, x1, y1 in _normalized_regions(regions):
        left = max(0, min(width - 1, int(round(x0 * width))))
        top = max(0, min(height - 1, int(round(y0 * height))))
        right = max(left + 1, min(width, int(round(x1 * width))))
        bottom = max(top + 1, min(height, int(round(y1 * height))))
        mask[top:bottom, left:right] = 255
        resolved.append(
            {
                "normalized": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
                "pixels": {
                    "left": left,
                    "top": top,
                    "right_exclusive": right,
                    "bottom_exclusive": bottom,
                    "width": right - left,
                    "height": bottom - top,
                },
            }
        )
    return mask, resolved


def _sample_unique_colors(image: np.ndarray, mask: np.ndarray) -> int:
    pixels = image[mask != 0]
    if pixels.size == 0:
        return 0
    maximum_samples = 50_000
    stride = max(1, int(np.ceil(pixels.shape[0] / maximum_samples)))
    sampled = pixels[::stride].astype(np.uint32, copy=False)
    packed = sampled[:, 0] | (sampled[:, 1] << 8) | (sampled[:, 2] << 16)
    return int(np.unique(packed).size)


def _surface_metrics(
    image: np.ndarray,
    mask: np.ndarray,
    thresholds: SurfaceThresholds,
) -> dict[str, Any]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    values = gray[mask != 0]
    if values.size == 0:
        raise ValueError("video-only ROI mask contains no pixels")
    values_f = values.astype(np.float32)
    p01, p99 = np.percentile(values_f, (1.0, 99.0))
    luminance_range = float(p99 - p01)
    black_pixel_ratio = float(np.mean(values <= thresholds.black_luminance_ceiling))
    luminance_stddev = float(np.std(values_f))
    mean_luminance = float(np.mean(values_f))
    unique_colors = _sample_unique_colors(image, mask)
    laplacian = cv2.Laplacian(gray, cv2.CV_32F)
    laplacian_variance = float(np.var(laplacian[mask != 0]))

    nonblack = (
        black_pixel_ratio <= thresholds.maximum_black_pixel_ratio
        and mean_luminance >= thresholds.minimum_mean_luminance
        and float(p99) > thresholds.black_luminance_ceiling
    )
    nonuniform = (
        luminance_range >= thresholds.minimum_luminance_range
        and luminance_stddev >= thresholds.minimum_luminance_stddev
        and unique_colors >= thresholds.minimum_unique_sampled_colors
    )
    return {
        "roi_pixel_count": int(values.size),
        "black_pixel_ratio": black_pixel_ratio,
        "mean_luminance": mean_luminance,
        "luminance_p01": float(p01),
        "luminance_p99": float(p99),
        "luminance_p99_minus_p01": luminance_range,
        "luminance_stddev": luminance_stddev,
        "unique_sampled_colors": unique_colors,
        "laplacian_variance": laplacian_variance,
        "nonblack": bool(nonblack),
        "nonuniform": bool(nonuniform),
        "visible_video_surface": bool(nonblack and nonuniform),
    }


def _dhash_for_regions(
    gray: np.ndarray,
    resolved_regions: Sequence[dict[str, Any]],
) -> np.ndarray:
    bits: list[np.ndarray] = []
    for region in resolved_regions:
        pixels = region["pixels"]
        crop = gray[
            pixels["top"] : pixels["bottom_exclusive"],
            pixels["left"] : pixels["right_exclusive"],
        ]
        resized = cv2.resize(crop, (9, 8), interpolation=cv2.INTER_AREA)
        bits.append((resized[:, 1:] > resized[:, :-1]).reshape(-1))
    return np.concatenate(bits)


def _temporal_metrics(
    image_a: np.ndarray,
    image_b: np.ndarray,
    mask: np.ndarray,
    resolved_regions: Sequence[dict[str, Any]],
    thresholds: SurfaceThresholds,
) -> dict[str, Any]:
    gray_a = cv2.cvtColor(image_a, cv2.COLOR_BGR2GRAY)
    gray_b = cv2.cvtColor(image_b, cv2.COLOR_BGR2GRAY)
    delta = cv2.absdiff(gray_a, gray_b)[mask != 0]
    changed_pixel_ratio = float(np.mean(delta > thresholds.temporal_delta_threshold))
    mean_absolute_difference = float(np.mean(delta.astype(np.float32)))
    hash_a = _dhash_for_regions(gray_a, resolved_regions)
    hash_b = _dhash_for_regions(gray_b, resolved_regions)
    dhash_distance = int(np.count_nonzero(hash_a != hash_b))
    observed = (
        changed_pixel_ratio >= thresholds.temporal_changed_pixel_ratio
        or mean_absolute_difference >= thresholds.temporal_mean_absolute_difference
        or dhash_distance >= thresholds.temporal_dhash_distance
    )
    return {
        "delta_luminance_threshold": thresholds.temporal_delta_threshold,
        "changed_pixel_ratio": changed_pixel_ratio,
        "mean_absolute_difference": mean_absolute_difference,
        "dhash_hamming_distance": dhash_distance,
        "temporal_pixel_change_observed": bool(observed),
        "required_for_gate": False,
        "reason_not_required": (
            "the bounded Gazebo VTOL acceptance scene is nearly static; signed native "
            "post-adoption sequence advancement supplies the live transport binding"
        ),
    }


def _capture_identity(
    path: Path,
    image: np.ndarray,
    sequence: int,
    captured_at_utc: str | None,
    metrics: dict[str, Any],
) -> dict[str, Any]:
    height, width = image.shape[:2]
    return {
        "path": str(path.resolve()),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "width": int(width),
        "height": int(height),
        "post_adoption_type24_sequence_after_capture": int(sequence),
        "captured_at_utc": captured_at_utc,
        "video_roi_metrics": metrics,
    }


def build_evidence(
    capture_a_path: Path,
    capture_b_path: Path,
    *,
    sequence_a: int,
    sequence_b: int,
    rtsp_streaming_proved: bool,
    rtsp_decoding_proved: bool,
    rtsp_width: int,
    rtsp_height: int,
    minimum_sequence_advance: int = 5,
    capture_a_at_utc: str | None = None,
    capture_b_at_utc: str | None = None,
    regions: Sequence[Sequence[float]] = DEFAULT_VIDEO_REGIONS,
    thresholds: SurfaceThresholds = DEFAULT_THRESHOLDS,
) -> dict[str, Any]:
    if sequence_a < 0 or sequence_b < 0:
        raise ValueError("post-adoption type-24 sequences cannot be negative")
    if minimum_sequence_advance < 1:
        raise ValueError("minimum sequence advancement must be positive")
    if rtsp_width < 1 or rtsp_height < 1:
        raise ValueError("RTSP resolution must be positive")

    capture_a_path = capture_a_path.resolve()
    capture_b_path = capture_b_path.resolve()
    image_a = _load_capture(capture_a_path)
    image_b = _load_capture(capture_b_path)
    height_a, width_a = image_a.shape[:2]
    height_b, width_b = image_b.shape[:2]
    mask_a, resolved_a = _mask_for_regions(height_a, width_a, regions)
    mask_b, resolved_b = _mask_for_regions(height_b, width_b, regions)
    metrics_a = _surface_metrics(image_a, mask_a, thresholds)
    metrics_b = _surface_metrics(image_b, mask_b, thresholds)

    same_geometry = (width_a, height_a) == (width_b, height_b)
    minimum_geometry = width_a >= 640 and height_a >= 480 and width_b >= 640 and height_b >= 480
    sequence_advance = sequence_b - sequence_a
    sequence_advanced = sequence_advance >= minimum_sequence_advance
    temporal: dict[str, Any]
    if same_geometry:
        temporal = _temporal_metrics(image_a, image_b, mask_a, resolved_a, thresholds)
    else:
        temporal = {
            "delta_luminance_threshold": thresholds.temporal_delta_threshold,
            "changed_pixel_ratio": None,
            "mean_absolute_difference": None,
            "dhash_hamming_distance": None,
            "temporal_pixel_change_observed": False,
            "required_for_gate": False,
            "unavailable_reason": "capture geometry differs",
        }

    checks = {
        "rtsp_streaming_proved": bool(rtsp_streaming_proved),
        "rtsp_decoding_proved": bool(rtsp_decoding_proved),
        "same_exact_client_geometry": bool(same_geometry),
        "minimum_exact_client_geometry": bool(minimum_geometry),
        "capture_a_video_roi_nonblack_nonuniform": bool(metrics_a["visible_video_surface"]),
        "capture_b_video_roi_nonblack_nonuniform": bool(metrics_b["visible_video_surface"]),
        "post_adoption_type24_sequence_advanced": bool(sequence_advanced),
    }
    failure_reasons = [name for name, passed in checks.items() if not passed]
    gate_passed = not failure_reasons
    normalized = _normalized_regions(regions)
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "evaluated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "gate_passed": gate_passed,
        "failure_reasons": failure_reasons,
        "checks": checks,
        "capture_method_required": (
            "powershell_exact_pid_hwnd_desktop_copyfromscreen_exact_client"
        ),
        "video_roi": {
            "strategy": "normalized_multi_region_excluding_native_qgc_ui_chrome",
            "normalized_regions": [
                {"x0": x0, "y0": y0, "x1": x1, "y1": y1} for x0, y0, x1, y1 in normalized
            ],
            "capture_a_resolved_regions": resolved_a,
            "capture_b_resolved_regions": resolved_b,
        },
        "thresholds": {
            "maximum_black_pixel_ratio": thresholds.maximum_black_pixel_ratio,
            "minimum_mean_luminance": thresholds.minimum_mean_luminance,
            "minimum_luminance_range": thresholds.minimum_luminance_range,
            "minimum_luminance_stddev": thresholds.minimum_luminance_stddev,
            "minimum_unique_sampled_colors": thresholds.minimum_unique_sampled_colors,
            "black_luminance_ceiling": thresholds.black_luminance_ceiling,
        },
        "rtsp": {
            "streaming_active_log_proved": bool(rtsp_streaming_proved),
            "decoding_active_log_proved": bool(rtsp_decoding_proved),
            "advertised_width": int(rtsp_width),
            "advertised_height": int(rtsp_height),
        },
        "runtime_binding": {
            "message_type": 24,
            "scope": "native_controller_post_adoption_only",
            "sequence_a": int(sequence_a),
            "sequence_b": int(sequence_b),
            "sequence_advance": int(sequence_advance),
            "minimum_sequence_advance": int(minimum_sequence_advance),
            "sequence_advanced": bool(sequence_advanced),
        },
        "capture_a": _capture_identity(
            capture_a_path, image_a, sequence_a, capture_a_at_utc, metrics_a
        ),
        "capture_b": _capture_identity(
            capture_b_path, image_b, sequence_b, capture_b_at_utc, metrics_b
        ),
        "temporal_diagnostic": temporal,
        "transport_bound_visible_video_surface_proved": gate_passed,
        "temporal_source_motion_proved": bool(
            gate_passed and temporal["temporal_pixel_change_observed"]
        ),
        "exact_source_frame_identity_proved": False,
        "qml_rendering_proved": False,
        "operator_visual_acceptance": "not_claimed",
    }


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path = path.resolve()
    if path.exists():
        raise ValueError(f"refusing to overwrite desktop-capture evidence: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-a", type=Path, required=True)
    parser.add_argument("--capture-b", type=Path, required=True)
    parser.add_argument("--sequence-a", type=int, required=True)
    parser.add_argument("--sequence-b", type=int, required=True)
    parser.add_argument("--capture-a-at-utc")
    parser.add_argument("--capture-b-at-utc")
    parser.add_argument("--minimum-sequence-advance", type=int, default=5)
    parser.add_argument("--rtsp-streaming-proved", action="store_true")
    parser.add_argument("--rtsp-decoding-proved", action="store_true")
    parser.add_argument("--rtsp-width", type=int, default=1280)
    parser.add_argument("--rtsp-height", type=int, default=960)
    parser.add_argument("--result-out", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    evidence = build_evidence(
        args.capture_a,
        args.capture_b,
        sequence_a=args.sequence_a,
        sequence_b=args.sequence_b,
        rtsp_streaming_proved=args.rtsp_streaming_proved,
        rtsp_decoding_proved=args.rtsp_decoding_proved,
        rtsp_width=args.rtsp_width,
        rtsp_height=args.rtsp_height,
        minimum_sequence_advance=args.minimum_sequence_advance,
        capture_a_at_utc=args.capture_a_at_utc,
        capture_b_at_utc=args.capture_b_at_utc,
    )
    _write_json_atomic(args.result_out, evidence)
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
    return 0 if evidence["gate_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
