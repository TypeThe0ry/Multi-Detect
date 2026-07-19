from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "capture_charuco_calibration.py"
SPEC = importlib.util.spec_from_file_location("capture_charuco_calibration", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_board_view_signature_is_normalized_and_detects_novel_pose() -> None:
    centered = np.asarray([[400, 250], [600, 250], [600, 350], [400, 350]], dtype=np.float32)
    shifted = centered + np.asarray([250, 100], dtype=np.float32)

    first = MODULE.board_view_signature(centered, width=1000, height=600)
    second = MODULE.board_view_signature(shifted, width=1000, height=600)

    assert first.center_x == pytest.approx(0.5)
    assert first.center_y == pytest.approx(0.5)
    assert MODULE.is_novel_view(first, [], minimum_distance=0.05)
    assert not MODULE.is_novel_view(first, [first], minimum_distance=0.05)
    assert MODULE.is_novel_view(second, [first], minimum_distance=0.05)


def test_board_view_signature_rejects_invalid_geometry() -> None:
    with pytest.raises(ValueError, match="at least three"):
        MODULE.board_view_signature(np.zeros((2, 2), dtype=np.float32), width=1280, height=720)
