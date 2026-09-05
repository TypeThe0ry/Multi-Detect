"""Validate acceptance-only grabs of QGC's one native Fly View VideoOutput.

The capture hook is deliberately non-visual and fail-closed.  It is exposed by
the custom plugin only for ``--isolated-hil`` plus an explicit environment
opt-in, recursively finds the existing ``objectName == \"videoContent\"`` item,
and invokes that item's ``QQuickItem.grabToImage`` method.  It never creates a
second window, player, VideoOutput, or simulator-specific visual surface.

This validator proves that the native Qt Quick video item produced non-black,
non-uniform pixels while authenticated QGC type-24 acknowledgement sequences
advanced.  It does not claim exact source-frame identity, complete scene-graph
semantics, Windows compositor capture, or human operator visual acceptance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

SCHEMA = "multidetect.qgc-native-video-grab-proof"
SCHEMA_VERSION = 1
MARKER_PREFIX = "MULTIDETECT_HIL_VIDEO_GRAB "
MARKER_SCHEMA = "multidetect.qgc-hil-video-grab"
MARKER_SCHEMA_VERSION = 1
ROLE_A = "pip-swapped"
ROLE_B = "fixed-wing"
QT_GRAB_GEOMETRY_ROUNDING_TOLERANCE_PX = 1.0


@dataclass(frozen=True)
class SurfaceThresholds:
    maximum_black_pixel_ratio: float = 0.95
    minimum_mean_luminance: float = 12.0
    minimum_luminance_range: float = 2.0
    minimum_luminance_stddev: float = 0.75
    minimum_unique_sampled_colors: int = 4
    black_luminance_ceiling: int = 8
    minimum_width: int = 320
    minimum_height: int = 180


DEFAULT_THRESHOLDS = SurfaceThresholds()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _parse_utc(value: str, *, field: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field} is not an ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _canonical_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.resolve())))


def _load_image(path: Path) -> np.ndarray:
    if not path.is_file():
        raise ValueError(f"native VideoOutput capture is missing: {path}")
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError(f"OpenCV could not decode a BGR PNG: {path}")
    return image


def _sample_unique_colors(image: np.ndarray) -> int:
    pixels = image.reshape(-1, 3)
    maximum_samples = 50_000
    stride = max(1, int(np.ceil(pixels.shape[0] / maximum_samples)))
    sampled = pixels[::stride].astype(np.uint32, copy=False)
    packed = sampled[:, 0] | (sampled[:, 1] << 8) | (sampled[:, 2] << 16)
    return int(np.unique(packed).size)


def _surface_metrics(
    image: np.ndarray,
    thresholds: SurfaceThresholds,
) -> dict[str, Any]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    values = gray.reshape(-1).astype(np.float32)
    p01, p99 = np.percentile(values, (1.0, 99.0))
    black_pixel_ratio = float(
        np.mean(values <= float(thresholds.black_luminance_ceiling))
    )
    mean_luminance = float(np.mean(values))
    luminance_stddev = float(np.std(values))
    luminance_range = float(p99 - p01)
    unique_colors = _sample_unique_colors(image)
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
        "pixel_count": int(values.size),
        "black_pixel_ratio": black_pixel_ratio,
        "mean_luminance": mean_luminance,
        "luminance_p01": float(p01),
        "luminance_p99": float(p99),
        "luminance_p99_minus_p01": luminance_range,
        "luminance_stddev": luminance_stddev,
        "unique_sampled_colors": unique_colors,
        "nonblack": bool(nonblack),
        "nonuniform": bool(nonuniform),
        "native_video_pixels_present": bool(nonblack and nonuniform),
    }


def _parse_markers(log_paths: Sequence[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for log_path in log_paths:
        if not log_path.is_file():
            continue
        text = log_path.read_text(encoding="utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            marker_index = line.find(MARKER_PREFIX)
            if marker_index < 0:
                continue
            candidate = line[marker_index + len(MARKER_PREFIX) :].strip()
            end = candidate.rfind("}")
            if end < 0:
                continue
            try:
                value = json.loads(candidate[: end + 1])
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict):
                continue
            value = dict(value)
            value["_log_path"] = str(log_path.resolve())
            value["_log_line"] = line_number
            records.append(value)
    return records


def _selected_marker(
    records: Sequence[dict[str, Any]],
    *,
    role: str,
) -> tuple[dict[str, Any] | None, int]:
    candidates: list[dict[str, Any]] = []
    for record in records:
        try:
            schema_version = int(record.get("schema_version", -1))
            sequence = int(record.get("sequence", -1))
        except (TypeError, ValueError):
            continue
        if (
            record.get("schema") != MARKER_SCHEMA
            or schema_version != MARKER_SCHEMA_VERSION
            or record.get("role") != role
            or sequence < 1
        ):
            continue
        candidates.append(record)
    if not candidates:
        return None, 0
    return max(candidates, key=lambda record: int(record["sequence"])), len(candidates)


def _marker_checks(
    marker: dict[str, Any] | None,
    *,
    role: str,
    capture_path: Path,
    image: np.ndarray,
    qgc_started_at: datetime,
    expected_decoded_width: int,
    expected_decoded_height: int,
) -> dict[str, bool]:
    if marker is None:
        return {
            "present": False,
            "role_matches": False,
            "path_matches": False,
            "sequence_positive": False,
            "timestamp_bound_to_qgc_process": False,
            "one_native_video_output_matched": False,
            "native_object_name_matches": False,
            "grab_queued": False,
            "grab_saved": False,
            "decoding_active": False,
            "output_visible": False,
            "video_full_state": False,
            "fixed_wing_state_matches_role": False,
            "output_geometry_positive": False,
            "device_pixel_ratio_positive_finite": False,
            "capture_geometry_matches_png": False,
            "capture_geometry_matches_logical_output_and_dpr": False,
            "decoded_geometry_matches": False,
        }

    height, width = image.shape[:2]
    try:
        recorded_at = _parse_utc(str(marker.get("recorded_at_utc", "")), field="recorded_at_utc")
    except ValueError:
        recorded_at = datetime.min.replace(tzinfo=timezone.utc)
    try:
        marker_path_matches = _canonical_path(
            Path(str(marker.get("path", "")))
        ) == _canonical_path(capture_path)
    except (OSError, ValueError):
        marker_path_matches = False
    try:
        output_width = float(marker.get("output_width", 0))
        output_height = float(marker.get("output_height", 0))
    except (TypeError, ValueError):
        output_width = output_height = 0.0
    try:
        device_pixel_ratio = float(marker.get("device_pixel_ratio", float("nan")))
    except (TypeError, ValueError):
        device_pixel_ratio = float("nan")
    try:
        capture_width = int(marker.get("capture_width", -1))
        capture_height = int(marker.get("capture_height", -1))
    except (TypeError, ValueError):
        capture_width = capture_height = -1
    try:
        decoded_width = int(marker.get("decoded_width", -1))
        decoded_height = int(marker.get("decoded_height", -1))
    except (TypeError, ValueError):
        decoded_width = decoded_height = -1
    try:
        marker_sequence = int(marker.get("sequence", -1))
        match_count = int(marker.get("native_output_match_count", -1))
    except (TypeError, ValueError):
        marker_sequence = match_count = -1

    fixed_wing_state_ok = (
        bool(marker.get("vtol_in_fwd_flight")) if role == ROLE_B else True
    )
    output_geometry_positive = (
        math.isfinite(output_width)
        and math.isfinite(output_height)
        and output_width > 0.0
        and output_height > 0.0
    )
    device_pixel_ratio_positive_finite = (
        math.isfinite(device_pixel_ratio) and device_pixel_ratio > 0.0
    )
    capture_geometry_matches_logical_output_and_dpr = (
        output_geometry_positive
        and device_pixel_ratio_positive_finite
        and capture_width > 0
        and capture_height > 0
        and abs(capture_width - (output_width * device_pixel_ratio))
        <= QT_GRAB_GEOMETRY_ROUNDING_TOLERANCE_PX
        and abs(capture_height - (output_height * device_pixel_ratio))
        <= QT_GRAB_GEOMETRY_ROUNDING_TOLERANCE_PX
    )
    return {
        "present": True,
        "role_matches": marker.get("role") == role,
        "path_matches": marker_path_matches,
        "sequence_positive": marker_sequence >= 1,
        "timestamp_bound_to_qgc_process": recorded_at >= qgc_started_at,
        "one_native_video_output_matched": match_count == 1,
        "native_object_name_matches": marker.get("object_name") == "videoContent",
        "grab_queued": marker.get("queued") is True,
        "grab_saved": marker.get("saved") is True,
        "decoding_active": marker.get("decoding") is True,
        "output_visible": marker.get("output_visible") is True,
        "video_full_state": marker.get("video_full_state") is True,
        "fixed_wing_state_matches_role": fixed_wing_state_ok,
        "output_geometry_positive": output_geometry_positive,
        "device_pixel_ratio_positive_finite": device_pixel_ratio_positive_finite,
        "capture_geometry_matches_png": capture_width == width and capture_height == height,
        "capture_geometry_matches_logical_output_and_dpr": (
            capture_geometry_matches_logical_output_and_dpr
        ),
        "decoded_geometry_matches": (
            decoded_width == expected_decoded_width
            and decoded_height == expected_decoded_height
        ),
    }


def _capture_identity(
    path: Path,
    image: np.ndarray,
    marker: dict[str, Any] | None,
    marker_count: int,
    marker_checks: dict[str, bool],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    height, width = image.shape[:2]
    return {
        "path": str(path.resolve()),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "width": int(width),
        "height": int(height),
        "marker_record_count": marker_count,
        "selected_marker": marker,
        "marker_checks": marker_checks,
        "surface_metrics": metrics,
    }


def build_evidence(
    capture_a_path: Path,
    capture_b_path: Path,
    *,
    log_paths: Sequence[Path],
    qgc_started_at_utc: str,
    sequence_a: int,
    sequence_b: int,
    minimum_sequence_advance: int = 5,
    expected_decoded_width: int = 1280,
    expected_decoded_height: int = 960,
    thresholds: SurfaceThresholds = DEFAULT_THRESHOLDS,
) -> dict[str, Any]:
    if sequence_a < 1 or sequence_b < 1:
        raise ValueError("post-adoption type-24 sequences must be positive")
    if minimum_sequence_advance < 1:
        raise ValueError("minimum sequence advancement must be positive")
    if expected_decoded_width < 1 or expected_decoded_height < 1:
        raise ValueError("expected decoded geometry must be positive")

    qgc_started_at = _parse_utc(qgc_started_at_utc, field="qgc_started_at_utc")
    capture_a_path = capture_a_path.resolve()
    capture_b_path = capture_b_path.resolve()
    image_a = _load_image(capture_a_path)
    image_b = _load_image(capture_b_path)
    metrics_a = _surface_metrics(image_a, thresholds)
    metrics_b = _surface_metrics(image_b, thresholds)
    records = _parse_markers(log_paths)
    marker_a, marker_a_count = _selected_marker(records, role=ROLE_A)
    marker_b, marker_b_count = _selected_marker(records, role=ROLE_B)
    checks_a = _marker_checks(
        marker_a,
        role=ROLE_A,
        capture_path=capture_a_path,
        image=image_a,
        qgc_started_at=qgc_started_at,
        expected_decoded_width=expected_decoded_width,
        expected_decoded_height=expected_decoded_height,
    )
    checks_b = _marker_checks(
        marker_b,
        role=ROLE_B,
        capture_path=capture_b_path,
        image=image_b,
        qgc_started_at=qgc_started_at,
        expected_decoded_width=expected_decoded_width,
        expected_decoded_height=expected_decoded_height,
    )

    height_a, width_a = image_a.shape[:2]
    height_b, width_b = image_b.shape[:2]
    sequence_advance = sequence_b - sequence_a
    try:
        marker_ordered = (
            marker_a is not None
            and marker_b is not None
            and int(marker_b["sequence"]) > int(marker_a["sequence"])
            and _parse_utc(
                str(marker_b["recorded_at_utc"]),
                field="marker_b.recorded_at_utc",
            )
            >= _parse_utc(
                str(marker_a["recorded_at_utc"]),
                field="marker_a.recorded_at_utc",
            )
        )
    except (KeyError, TypeError, ValueError):
        marker_ordered = False
    file_timestamps_bound = (
        datetime.fromtimestamp(capture_a_path.stat().st_mtime, tz=timezone.utc)
        >= qgc_started_at
        and datetime.fromtimestamp(capture_b_path.stat().st_mtime, tz=timezone.utc)
        >= qgc_started_at
    )
    checks: dict[str, bool] = {
        "capture_a_marker_valid": all(checks_a.values()),
        "capture_b_marker_valid": all(checks_b.values()),
        "marker_sequence_and_time_ordered": marker_ordered,
        "capture_files_timestamp_bound_to_qgc_process": file_timestamps_bound,
        "capture_a_minimum_geometry": (
            width_a >= thresholds.minimum_width and height_a >= thresholds.minimum_height
        ),
        "capture_b_minimum_geometry": (
            width_b >= thresholds.minimum_width and height_b >= thresholds.minimum_height
        ),
        "same_native_item_capture_geometry": (width_a, height_a) == (width_b, height_b),
        "capture_a_native_video_pixels_present": bool(metrics_a["native_video_pixels_present"]),
        "capture_b_native_video_pixels_present": bool(metrics_b["native_video_pixels_present"]),
        "post_adoption_type24_sequence_advanced": (
            sequence_advance >= minimum_sequence_advance
        ),
    }
    failure_reasons = [name for name, passed in checks.items() if not passed]
    gate_passed = not failure_reasons
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "evaluated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "gate_passed": gate_passed,
        "failure_reasons": failure_reasons,
        "checks": checks,
        "capture_method": "native_qquickitem_grabToImage_existing_videoContent",
        "native_qgc_ui_contract": {
            "native_surface": "QGroundControl FlyView/FlyViewVideo/PipView/VideoOutput",
            "native_video_object_name": "videoContent",
            "new_window_created": False,
            "second_video_output_created": False,
            "independent_simulator_ui_created": False,
        },
        "thresholds": {
            "maximum_black_pixel_ratio": thresholds.maximum_black_pixel_ratio,
            "minimum_mean_luminance": thresholds.minimum_mean_luminance,
            "minimum_luminance_range": thresholds.minimum_luminance_range,
            "minimum_luminance_stddev": thresholds.minimum_luminance_stddev,
            "minimum_unique_sampled_colors": thresholds.minimum_unique_sampled_colors,
            "black_luminance_ceiling": thresholds.black_luminance_ceiling,
            "minimum_width": thresholds.minimum_width,
            "minimum_height": thresholds.minimum_height,
        },
        "runtime_binding": {
            "message_type": 24,
            "scope": "native_controller_post_adoption_only",
            "sequence_a": sequence_a,
            "sequence_b": sequence_b,
            "sequence_advance": sequence_advance,
            "minimum_sequence_advance": minimum_sequence_advance,
        },
        "capture_a": _capture_identity(
            capture_a_path, image_a, marker_a, marker_a_count, checks_a, metrics_a
        ),
        "capture_b": _capture_identity(
            capture_b_path, image_b, marker_b, marker_b_count, checks_b, metrics_b
        ),
        "native_qquickitem_video_pixels_proved": gate_passed,
        "transport_bound_native_qquickitem_video_surface_proved": gate_passed,
        "desktop_capture_gpu_surface_proved": False,
        "exact_source_frame_identity_proved": False,
        "complete_qml_rendering_semantics_proved": False,
        "operator_visual_acceptance": "not_claimed",
    }


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path = path.resolve()
    if path.exists():
        raise ValueError(f"refusing to overwrite native-video-grab evidence: {path}")
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
    parser.add_argument("--qgc-log", type=Path, action="append", required=True)
    parser.add_argument("--qgc-started-at-utc", required=True)
    parser.add_argument("--sequence-a", type=int, required=True)
    parser.add_argument("--sequence-b", type=int, required=True)
    parser.add_argument("--minimum-sequence-advance", type=int, default=5)
    parser.add_argument("--expected-decoded-width", type=int, default=1280)
    parser.add_argument("--expected-decoded-height", type=int, default=960)
    parser.add_argument("--result-out", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    evidence = build_evidence(
        args.capture_a,
        args.capture_b,
        log_paths=args.qgc_log,
        qgc_started_at_utc=args.qgc_started_at_utc,
        sequence_a=args.sequence_a,
        sequence_b=args.sequence_b,
        minimum_sequence_advance=args.minimum_sequence_advance,
        expected_decoded_width=args.expected_decoded_width,
        expected_decoded_height=args.expected_decoded_height,
    )
    _write_json_atomic(args.result_out, evidence)
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
    return 0 if evidence["gate_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
