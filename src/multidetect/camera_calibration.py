from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

_IMAGE_SUFFIXES = frozenset({".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff"})


@dataclass(frozen=True, slots=True)
class CharucoBoardSpec:
    squares_x: int = 7
    squares_y: int = 5
    square_length_m: float = 0.040
    marker_length_m: float = 0.020
    dictionary_name: str = "DICT_5X5_100"

    def __post_init__(self) -> None:
        if self.squares_x < 4 or self.squares_y < 4:
            raise ValueError("ChArUco board must contain at least 4x4 squares")
        if not math.isfinite(self.square_length_m) or self.square_length_m <= 0.0:
            raise ValueError("ChArUco square length must be positive")
        if (
            not math.isfinite(self.marker_length_m)
            or self.marker_length_m <= 0.0
            or self.marker_length_m >= self.square_length_m
        ):
            raise ValueError("ChArUco marker length must be positive and smaller than a square")
        if not hasattr(cv2.aruco, self.dictionary_name):
            raise ValueError(f"unknown ArUco dictionary: {self.dictionary_name}")

    def create(self) -> Any:
        dictionary_id = getattr(cv2.aruco, self.dictionary_name)
        dictionary = cv2.aruco.getPredefinedDictionary(dictionary_id)
        if hasattr(cv2.aruco, "CharucoBoard"):
            return cv2.aruco.CharucoBoard(
                (self.squares_x, self.squares_y),
                self.square_length_m,
                self.marker_length_m,
                dictionary,
            )
        return cv2.aruco.CharucoBoard_create(
            self.squares_x,
            self.squares_y,
            self.square_length_m,
            self.marker_length_m,
            dictionary,
        )


class CompatibleCharucoDetector:
    """One detector contract for current OpenCV and Jetson's older ArUco API."""

    def __init__(self, board_spec: CharucoBoardSpec) -> None:
        self.board = board_spec.create()
        dictionary_id = getattr(cv2.aruco, board_spec.dictionary_name)
        self.dictionary = cv2.aruco.getPredefinedDictionary(dictionary_id)
        detector_type = getattr(cv2.aruco, "CharucoDetector", None)
        self._modern = detector_type(self.board) if detector_type is not None else None
        marker_detector_type = getattr(cv2.aruco, "ArucoDetector", None)
        self._marker_detector = (
            marker_detector_type(self.dictionary) if marker_detector_type is not None else None
        )

    def detect_board(
        self,
        image: np.ndarray,
    ) -> tuple[Any | None, Any | None, Any, Any | None]:
        if self._modern is not None:
            return self._modern.detectBoard(image)
        legacy_detect = getattr(cv2.aruco, "detectMarkers", None)
        if legacy_detect is not None:
            marker_corners, marker_ids, rejected = legacy_detect(image, self.dictionary)
        elif self._marker_detector is not None:
            marker_corners, marker_ids, rejected = self._marker_detector.detectMarkers(image)
        else:  # pragma: no cover - unsupported partial OpenCV contrib builds.
            raise RuntimeError("OpenCV ArUco marker detector is missing")
        if marker_ids is None or not marker_corners:
            return None, None, rejected, marker_ids
        _count, charuco_corners, charuco_ids = cv2.aruco.interpolateCornersCharuco(
            marker_corners,
            marker_ids,
            image,
            self.board,
        )
        return charuco_corners, charuco_ids, marker_corners, marker_ids


def _charuco_chessboard_corners(board: Any) -> np.ndarray:
    getter = getattr(board, "getChessboardCorners", None)
    values = getter() if getter is not None else getattr(board, "chessboardCorners", None)
    if values is None:
        raise RuntimeError("OpenCV ChArUco board corner geometry is missing")
    return np.asarray(values, dtype=np.float32)


def _charuco_generate_image(
    board: Any,
    size: tuple[int, int],
    *,
    margin_size: int,
    border_bits: int,
) -> np.ndarray:
    generator = getattr(board, "generateImage", None)
    if generator is not None:
        return generator(size, marginSize=margin_size, borderBits=border_bits)
    drawer = getattr(board, "draw", None)
    if drawer is None:
        raise RuntimeError("OpenCV ChArUco board renderer is missing")
    return drawer(size, marginSize=margin_size, borderBits=border_bits)


@dataclass(frozen=True, slots=True)
class CalibrationQualityThresholds:
    minimum_accepted_frames: int = 20
    minimum_corners_per_frame: int = 12
    grid_columns: int = 4
    grid_rows: int = 3
    minimum_grid_coverage: float = 0.65
    minimum_median_board_area_fraction: float = 0.04
    maximum_rms_reprojection_error_px: float = 0.80
    maximum_per_view_error_px: float = 1.50
    maximum_focal_std_fraction: float = 0.05
    minimum_normalized_focal_length: float = 0.20
    maximum_normalized_focal_length: float = 8.0
    maximum_principal_point_offset_fraction: float = 0.25
    minimum_board_area_ratio: float = 1.8
    minimum_pose_tilt_span_deg: float = 12.0

    def __post_init__(self) -> None:
        if self.minimum_accepted_frames < 10 or self.minimum_corners_per_frame < 6:
            raise ValueError("camera calibration needs enough frames and corners")
        if self.grid_columns < 2 or self.grid_rows < 2:
            raise ValueError("camera calibration grid must be at least 2x2")
        fractions = (
            self.minimum_grid_coverage,
            self.minimum_median_board_area_fraction,
            self.maximum_focal_std_fraction,
            self.minimum_normalized_focal_length,
            self.maximum_principal_point_offset_fraction,
        )
        if any(not math.isfinite(value) or not 0.0 < value <= 1.0 for value in fractions):
            raise ValueError("camera calibration fractions must be in (0, 1]")
        errors = (
            self.maximum_rms_reprojection_error_px,
            self.maximum_per_view_error_px,
        )
        if any(not math.isfinite(value) or value <= 0.0 for value in errors):
            raise ValueError("camera calibration error limits must be positive")
        if (
            not math.isfinite(self.maximum_normalized_focal_length)
            or self.maximum_normalized_focal_length <= self.minimum_normalized_focal_length
        ):
            raise ValueError("maximum normalized focal length must exceed the minimum")
        if not math.isfinite(self.minimum_board_area_ratio) or self.minimum_board_area_ratio <= 1.0:
            raise ValueError("camera calibration needs meaningful distance-scale variation")
        if not math.isfinite(self.minimum_pose_tilt_span_deg) or not (
            0.0 < self.minimum_pose_tilt_span_deg <= 90.0
        ):
            raise ValueError("camera calibration pose-tilt span must be in (0, 90]")


@dataclass(frozen=True, slots=True)
class FrameObservation:
    path: Path
    image_points: np.ndarray
    object_points: np.ndarray
    board_area_fraction: float


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary_name).replace(path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def discover_images(directory: Path) -> tuple[Path, ...]:
    if not directory.is_dir():
        raise ValueError(f"calibration image directory does not exist: {directory}")
    images = tuple(
        sorted(
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES
        )
    )
    if not images:
        raise ValueError("calibration image directory contains no supported images")
    return images


def _board_area_fraction(points: np.ndarray, width: int, height: int) -> float:
    if len(points) < 3:
        return 0.0
    hull = cv2.convexHull(points.astype(np.float32).reshape(-1, 1, 2))
    return float(cv2.contourArea(hull) / float(width * height))


def _grid_coverage(
    observations: Sequence[FrameObservation],
    *,
    width: int,
    height: int,
    columns: int,
    rows: int,
) -> float:
    occupied: set[tuple[int, int]] = set()
    for observation in observations:
        for x, y in observation.image_points.reshape(-1, 2):
            column = min(columns - 1, max(0, int(float(x) / width * columns)))
            row = min(rows - 1, max(0, int(float(y) / height * rows)))
            occupied.add((column, row))
    return len(occupied) / float(columns * rows)


def _detect_observations(
    image_paths: Sequence[Path],
    *,
    board_spec: CharucoBoardSpec,
    minimum_corners: int,
) -> tuple[tuple[FrameObservation, ...], tuple[dict[str, str], ...], tuple[int, int]]:
    detector = CompatibleCharucoDetector(board_spec)
    board = detector.board
    chessboard_corners = _charuco_chessboard_corners(board)
    observations: list[FrameObservation] = []
    rejected: list[dict[str, str]] = []
    image_size: tuple[int, int] | None = None

    for path in image_paths:
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            rejected.append({"file": path.name, "reason": "decode_failed"})
            continue
        height, width = image.shape[:2]
        current_size = (width, height)
        if image_size is None:
            image_size = current_size
        elif image_size != current_size:
            rejected.append({"file": path.name, "reason": "resolution_mismatch"})
            continue
        corners, ids, _marker_corners, _marker_ids = detector.detect_board(image)
        if corners is None or ids is None or len(ids) < minimum_corners:
            rejected.append({"file": path.name, "reason": "insufficient_charuco_corners"})
            continue
        ids_flat = np.asarray(ids, dtype=np.int32).reshape(-1)
        image_points = np.asarray(corners, dtype=np.float32).reshape(-1, 2)
        object_points = chessboard_corners[ids_flat].reshape(-1, 3)
        observations.append(
            FrameObservation(
                path=path,
                image_points=image_points,
                object_points=object_points,
                board_area_fraction=_board_area_fraction(image_points, width, height),
            )
        )

    if image_size is None:
        raise ValueError("none of the calibration images could be decoded")
    return tuple(observations), tuple(rejected), image_size


def calibrate_charuco_directory(
    *,
    image_directory: Path,
    calibration_id: str,
    board_spec: CharucoBoardSpec,
    thresholds: CalibrationQualityThresholds,
    mount_pitch_down_deg: float,
    mount_yaw_right_deg: float,
    mount_roll_clockwise_deg: float,
    boresight_sigma_deg: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not calibration_id.strip():
        raise ValueError("calibration ID cannot be empty")
    mount_values = (
        mount_pitch_down_deg,
        mount_yaw_right_deg,
        mount_roll_clockwise_deg,
        boresight_sigma_deg,
    )
    if not all(math.isfinite(value) for value in mount_values):
        raise ValueError("camera mount values must be finite")
    if not 0.0 < boresight_sigma_deg <= 10.0:
        raise ValueError("boresight uncertainty must be in (0, 10] degrees")

    image_paths = discover_images(image_directory)
    observations, rejected, (width, height) = _detect_observations(
        image_paths,
        board_spec=board_spec,
        minimum_corners=thresholds.minimum_corners_per_frame,
    )
    if len(observations) < 3:
        raise ValueError("fewer than three usable ChArUco views were detected")

    object_points = [item.object_points for item in observations]
    image_points = [item.image_points for item in observations]
    result = cv2.calibrateCameraExtended(
        object_points,
        image_points,
        (width, height),
        None,
        None,
    )
    rms, camera_matrix, distortion = result[:3]
    rotation_vectors = result[3]
    intrinsic_std = np.asarray(result[5], dtype=np.float64).reshape(-1)
    per_view_errors = np.asarray(result[7], dtype=np.float64).reshape(-1)
    fx, fy = float(camera_matrix[0, 0]), float(camera_matrix[1, 1])
    focal_std_fraction = max(
        float(intrinsic_std[0]) / fx,
        float(intrinsic_std[1]) / fy,
    )
    grid_coverage = _grid_coverage(
        observations,
        width=width,
        height=height,
        columns=thresholds.grid_columns,
        rows=thresholds.grid_rows,
    )
    median_area = float(np.median([item.board_area_fraction for item in observations]))
    minimum_area = min(item.board_area_fraction for item in observations)
    maximum_area = max(item.board_area_fraction for item in observations)
    board_area_ratio = maximum_area / max(minimum_area, 1e-12)
    maximum_view_error = float(np.max(per_view_errors))
    focal_scale = max(width, height)
    minimum_normalized_focal = min(fx, fy) / focal_scale
    maximum_normalized_focal = max(fx, fy) / focal_scale
    principal_point_offset = max(
        abs(float(camera_matrix[0, 2]) - width * 0.5) / width,
        abs(float(camera_matrix[1, 2]) - height * 0.5) / height,
    )
    pose_tilts = []
    for rotation_vector in rotation_vectors:
        rotation, _ = cv2.Rodrigues(rotation_vector)
        cosine = min(1.0, max(-1.0, abs(float(rotation[2, 2]))))
        pose_tilts.append(math.degrees(math.acos(cosine)))
    pose_tilt_span = max(pose_tilts) - min(pose_tilts)

    gates = {
        "accepted_frame_count": len(observations) >= thresholds.minimum_accepted_frames,
        "grid_coverage": grid_coverage >= thresholds.minimum_grid_coverage,
        "median_board_area": median_area >= thresholds.minimum_median_board_area_fraction,
        "rms_reprojection_error": float(rms) <= thresholds.maximum_rms_reprojection_error_px,
        "maximum_per_view_error": maximum_view_error <= thresholds.maximum_per_view_error_px,
        "focal_std_fraction": focal_std_fraction <= thresholds.maximum_focal_std_fraction,
        "focal_length_physical_domain": (
            minimum_normalized_focal >= thresholds.minimum_normalized_focal_length
            and maximum_normalized_focal <= thresholds.maximum_normalized_focal_length
        ),
        "principal_point_domain": principal_point_offset
        <= thresholds.maximum_principal_point_offset_fraction,
        "board_distance_scale_diversity": board_area_ratio >= thresholds.minimum_board_area_ratio,
        "board_pose_tilt_diversity": pose_tilt_span >= thresholds.minimum_pose_tilt_span_deg,
    }
    passed = all(gates.values())
    coefficients = np.asarray(distortion, dtype=np.float64).reshape(-1)
    coefficients = np.pad(coefficients, (0, max(0, 5 - len(coefficients))))
    calibration = {
        "schema_version": 1,
        "calibration": {
            "calibration_id": calibration_id,
            "width_px": width,
            "height_px": height,
            "fx_px": fx,
            "fy_px": fy,
            "cx_px": float(camera_matrix[0, 2]),
            "cy_px": float(camera_matrix[1, 2]),
            "mount_pitch_down_deg": mount_pitch_down_deg,
            "mount_yaw_right_deg": mount_yaw_right_deg,
            "mount_roll_clockwise_deg": mount_roll_clockwise_deg,
            "k1": float(coefficients[0]),
            "k2": float(coefficients[1]),
            "p1": float(coefficients[2]),
            "p2": float(coefficients[3]),
            "k3": float(coefficients[4]),
            "boresight_sigma_deg": boresight_sigma_deg,
        },
    }
    report = {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "calibration_id": calibration_id,
        "passed": passed,
        "board": asdict(board_spec),
        "thresholds": asdict(thresholds),
        "metrics": {
            "input_frame_count": len(image_paths),
            "accepted_frame_count": len(observations),
            "rejected_frame_count": len(rejected),
            "rms_reprojection_error_px": float(rms),
            "maximum_per_view_error_px": maximum_view_error,
            "median_board_area_fraction": median_area,
            "grid_coverage": grid_coverage,
            "focal_std_fraction": focal_std_fraction,
            "minimum_normalized_focal_length": minimum_normalized_focal,
            "maximum_normalized_focal_length": maximum_normalized_focal,
            "principal_point_offset_fraction": principal_point_offset,
            "board_area_ratio": board_area_ratio,
            "pose_tilt_span_deg": pose_tilt_span,
        },
        "gates": gates,
        "rejected_frames": list(rejected),
        "accepted_frames": [
            {
                "file": item.path.name,
                "sha256": _sha256(item.path),
                "corner_count": len(item.image_points),
                "board_area_fraction": item.board_area_fraction,
                "reprojection_error_px": float(per_view_errors[index]),
            }
            for index, item in enumerate(observations)
        ],
        "mount_survey": {
            "pitch_down_deg": mount_pitch_down_deg,
            "yaw_right_deg": mount_yaw_right_deg,
            "roll_clockwise_deg": mount_roll_clockwise_deg,
            "boresight_sigma_deg": boresight_sigma_deg,
            "explicit_operator_input": True,
        },
        "capabilities": {
            "ranging_input_candidate": passed,
            "flight_control_enabled": False,
            "physical_release_enabled": False,
        },
    }
    return calibration, report


def render_board(
    *, board_spec: CharucoBoardSpec, output: Path, width_px: int, margin_px: int
) -> None:
    if width_px < 1000 or margin_px < 0:
        raise ValueError("board rendering width must be >=1000 and margin non-negative")
    aspect = board_spec.squares_y / board_spec.squares_x
    height_px = max(1, round(width_px * aspect))
    image = _charuco_generate_image(
        board_spec.create(),
        (width_px, height_px),
        margin_size=margin_px,
        border_bits=1,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output), image):
        raise ValueError(f"failed to write ChArUco board: {output}")


def _board_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--squares-x", type=int, default=7)
    parser.add_argument("--squares-y", type=int, default=5)
    parser.add_argument("--square-length-mm", type=float, default=40.0)
    parser.add_argument("--marker-length-mm", type=float, default=20.0)


def _spec(args: argparse.Namespace) -> CharucoBoardSpec:
    return CharucoBoardSpec(
        squares_x=args.squares_x,
        squares_y=args.squares_y,
        square_length_m=args.square_length_mm / 1000.0,
        marker_length_m=args.marker_length_mm / 1000.0,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Strict ChArUco camera calibration")
    commands = parser.add_subparsers(dest="command", required=True)
    board = commands.add_parser("board", help="render the exact board used for calibration")
    _board_arguments(board)
    board.add_argument("--output", type=Path, required=True)
    board.add_argument("--width-px", type=int, default=4200)
    board.add_argument("--margin-px", type=int, default=0)

    calibrate = commands.add_parser("calibrate", help="calibrate from a directory of images")
    _board_arguments(calibrate)
    calibrate.add_argument("--images", type=Path, required=True)
    calibrate.add_argument("--calibration-id", required=True)
    calibrate.add_argument("--mount-pitch-down-deg", type=float, required=True)
    calibrate.add_argument("--mount-yaw-right-deg", type=float, required=True)
    calibrate.add_argument("--mount-roll-clockwise-deg", type=float, required=True)
    calibrate.add_argument("--boresight-sigma-deg", type=float, required=True)
    calibrate.add_argument("--calibration-out", type=Path, required=True)
    calibrate.add_argument("--report-out", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    board_spec = _spec(args)
    if args.command == "board":
        render_board(
            board_spec=board_spec,
            output=args.output,
            width_px=args.width_px,
            margin_px=args.margin_px,
        )
        return 0
    calibration, report = calibrate_charuco_directory(
        image_directory=args.images,
        calibration_id=args.calibration_id,
        board_spec=board_spec,
        thresholds=CalibrationQualityThresholds(),
        mount_pitch_down_deg=args.mount_pitch_down_deg,
        mount_yaw_right_deg=args.mount_yaw_right_deg,
        mount_roll_clockwise_deg=args.mount_roll_clockwise_deg,
        boresight_sigma_deg=args.boresight_sigma_deg,
    )
    _atomic_json(args.report_out, report)
    if not report["passed"]:
        return 2
    _atomic_json(args.calibration_out, calibration)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
