from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from multidetect.camera_calibration import (
    CalibrationQualityThresholds,
    CharucoBoardSpec,
    CompatibleCharucoDetector,
    _charuco_chessboard_corners,
    _charuco_generate_image,
    _grid_coverage,
    calibrate_charuco_directory,
    discover_images,
    main,
    render_board,
)
from multidetect.multimodal_ranging import load_camera_calibration


def test_charuco_board_contract_rejects_invalid_geometry() -> None:
    with pytest.raises(ValueError, match="smaller than a square"):
        CharucoBoardSpec(square_length_m=0.02, marker_length_m=0.02)
    with pytest.raises(ValueError, match="at least 4x4"):
        CharucoBoardSpec(squares_x=3)


def test_quality_thresholds_reject_weak_capture_requirements() -> None:
    with pytest.raises(ValueError, match="enough frames"):
        CalibrationQualityThresholds(minimum_accepted_frames=3)
    with pytest.raises(ValueError, match="fractions"):
        CalibrationQualityThresholds(minimum_grid_coverage=0.0)


def test_rendered_board_is_detectable(tmp_path: Path) -> None:
    output = tmp_path / "charuco.png"
    spec = CharucoBoardSpec()
    render_board(board_spec=spec, output=output, width_px=1400, margin_px=40)
    image = cv2.imread(str(output), cv2.IMREAD_GRAYSCALE)
    corners, ids, _, _ = CompatibleCharucoDetector(spec).detect_board(image)
    assert corners is not None
    assert ids is not None
    assert len(ids) == (spec.squares_x - 1) * (spec.squares_y - 1)


def test_rendered_board_is_detectable_with_legacy_jetson_aruco_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "charuco-legacy.png"
    spec = CharucoBoardSpec()
    render_board(board_spec=spec, output=output, width_px=1400, margin_px=40)
    image = cv2.imread(str(output), cv2.IMREAD_GRAYSCALE)
    modern_detector = cv2.aruco.CharucoDetector(spec.create())

    def legacy_interpolate(marker_corners, marker_ids, source_image, board):
        del marker_corners, marker_ids, board
        corners, ids, _, _ = modern_detector.detectBoard(source_image)
        return len(ids), corners, ids

    monkeypatch.setattr(
        cv2.aruco,
        "interpolateCornersCharuco",
        legacy_interpolate,
        raising=False,
    )
    monkeypatch.delattr(cv2.aruco, "CharucoDetector")

    corners, ids, _, _ = CompatibleCharucoDetector(spec).detect_board(image)

    assert corners is not None
    assert ids is not None
    assert len(ids) == (spec.squares_x - 1) * (spec.squares_y - 1)


def test_legacy_jetson_charuco_board_geometry_and_renderer_properties() -> None:
    class LegacyBoard:
        chessboardCorners = np.asarray([[1.0, 2.0, 0.0]], dtype=np.float32)

        @staticmethod
        def draw(size, *, marginSize, borderBits):
            assert size == (12, 8)
            assert marginSize == 1
            assert borderBits == 1
            return np.full((8, 12), 255, dtype=np.uint8)

    board = LegacyBoard()
    assert _charuco_chessboard_corners(board).shape == (1, 3)
    assert _charuco_generate_image(
        board,
        (12, 8),
        margin_size=1,
        border_bits=1,
    ).shape == (8, 12)


def test_discover_images_is_sorted_and_rejects_empty_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no supported images"):
        discover_images(tmp_path)
    (tmp_path / "b.jpg").write_bytes(b"b")
    (tmp_path / "a.png").write_bytes(b"a")
    (tmp_path / "ignore.txt").write_text("x", encoding="utf-8")
    assert [item.name for item in discover_images(tmp_path)] == ["a.png", "b.jpg"]


def test_grid_coverage_counts_spatial_bins() -> None:
    from multidetect.camera_calibration import FrameObservation

    points = np.array([[5, 5], [95, 5], [5, 95], [95, 95]], dtype=np.float32)
    observation = FrameObservation(Path("frame.png"), points, np.zeros((4, 3)), 0.5)
    assert _grid_coverage((observation,), width=100, height=100, columns=2, rows=2) == 1.0


def test_board_cli_writes_requested_image(tmp_path: Path) -> None:
    output = tmp_path / "board.png"
    assert main(["board", "--output", str(output), "--width-px", "1200"]) == 0
    assert output.stat().st_size > 10_000


def test_strict_ranging_loader_accepts_calibrator_output_shape(tmp_path: Path) -> None:
    document = {
        "schema_version": 1,
        "calibration": {
            "calibration_id": "camera-main-test",
            "width_px": 1280,
            "height_px": 720,
            "fx_px": 900.0,
            "fy_px": 901.0,
            "cx_px": 640.0,
            "cy_px": 360.0,
            "mount_pitch_down_deg": 5.0,
            "mount_yaw_right_deg": 0.0,
            "mount_roll_clockwise_deg": 0.0,
            "k1": -0.1,
            "k2": 0.02,
            "p1": 0.0,
            "p2": 0.0,
            "k3": 0.0,
            "boresight_sigma_deg": 0.4,
        },
    }
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert load_camera_calibration(path).calibration_id == "camera-main-test"


def test_physical_projection_dataset_passes_and_recovers_intrinsics(tmp_path: Path) -> None:
    spec = CharucoBoardSpec()
    board_image = spec.create().generateImage((1400, 1000), marginSize=0, borderBits=1)
    board_height, board_width = board_image.shape
    source_corners = np.float32(
        [[0, 0], [board_width - 1, 0], [board_width - 1, board_height - 1], [0, board_height - 1]]
    )
    camera_matrix = np.array(
        [[900.0, 0.0, 640.0], [0.0, 910.0, 360.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    board_corners = np.float32([[0, 0, 0], [0.28, 0, 0], [0.28, 0.20, 0], [0, 0.20, 0]])
    random = np.random.default_rng(716)
    created = 0
    for _ in range(150):
        x_angle, y_angle, z_angle = np.radians(
            [random.uniform(-32, 32), random.uniform(-34, 34), random.uniform(-20, 20)]
        )
        rotate_x = np.array(
            [
                [1, 0, 0],
                [0, np.cos(x_angle), -np.sin(x_angle)],
                [0, np.sin(x_angle), np.cos(x_angle)],
            ]
        )
        rotate_y = np.array(
            [
                [np.cos(y_angle), 0, np.sin(y_angle)],
                [0, 1, 0],
                [-np.sin(y_angle), 0, np.cos(y_angle)],
            ]
        )
        rotate_z = np.array(
            [
                [np.cos(z_angle), -np.sin(z_angle), 0],
                [np.sin(z_angle), np.cos(z_angle), 0],
                [0, 0, 1],
            ]
        )
        rotation_vector, _ = cv2.Rodrigues(rotate_z @ rotate_y @ rotate_x)
        translation = np.array(
            [
                [-0.14 + random.uniform(-0.12, 0.12)],
                [-0.10 + random.uniform(-0.06, 0.06)],
                [random.uniform(0.44, 0.76)],
            ]
        )
        destination, _ = cv2.projectPoints(
            board_corners,
            rotation_vector,
            translation,
            camera_matrix,
            np.zeros(5),
        )
        destination = destination.reshape(4, 2).astype(np.float32)
        if (
            destination[:, 0].min() < 5
            or destination[:, 0].max() > 1275
            or destination[:, 1].min() < 5
            or destination[:, 1].max() > 715
        ):
            continue
        transform = cv2.getPerspectiveTransform(source_corners, destination)
        view = cv2.warpPerspective(board_image, transform, (1280, 720), borderValue=255)
        assert cv2.imwrite(str(tmp_path / f"{created:03d}.png"), view)
        created += 1
        if created == 32:
            break
    assert created == 32

    calibration, report = calibrate_charuco_directory(
        image_directory=tmp_path,
        calibration_id="synthetic-physical-e2e",
        board_spec=spec,
        thresholds=CalibrationQualityThresholds(),
        mount_pitch_down_deg=0.0,
        mount_yaw_right_deg=0.0,
        mount_roll_clockwise_deg=0.0,
        boresight_sigma_deg=0.5,
    )

    assert report["passed"] is True
    assert all(report["gates"].values())
    assert calibration["calibration"]["fx_px"] == pytest.approx(900.0, rel=0.02)
    assert calibration["calibration"]["fy_px"] == pytest.approx(910.0, rel=0.02)
