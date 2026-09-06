from __future__ import annotations

import hashlib
from pathlib import Path

import cv2

from multidetect.camera_calibration import CharucoBoardSpec, CompatibleCharucoDetector

ASSET = Path(__file__).parents[1] / "configs" / "calibration" / "charuco-7x5-40mm-20mm-4200px.png"
EXPECTED_SHA256 = "66AAF16F10689E9EDFF68B4684128E028AD89B09AABC55AB9CDC4D02521B23AC"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def test_tracked_charuco_board_is_exact_and_detector_compatible() -> None:
    assert ASSET.is_file(), f"tracked ChArUco board is missing: {ASSET}"
    assert _sha256(ASSET) == EXPECTED_SHA256

    image = cv2.imread(str(ASSET), cv2.IMREAD_COLOR)
    assert image is not None
    assert image.shape[:2] == (3000, 4200)

    detector = CompatibleCharucoDetector(CharucoBoardSpec())
    corners, ids, _marker_corners, _marker_ids = detector.detect_board(image)
    assert corners is not None
    assert ids is not None
    assert len(ids) >= 12
