"""Tests for offline diagnostic geometry and trace boundary reconstruction only."""
import importlib.util
import math
from pathlib import Path

import numpy as np
import pytest


path = Path(__file__).resolve().parents[1] / 'scripts/study_x1_fw_envelope.py'
spec = importlib.util.spec_from_file_location('study_x1_fw_envelope', path)
study = importlib.util.module_from_spec(spec)
spec.loader.exec_module(study)


def test_capsule_keeps_corridor_support_but_drops_square_radius_corner():
    box, radius = study.stadium(np.zeros(2), np.zeros(2), 1068)
    assert box == (-1068, 1068, -1068, 1068)
    assert 1200 - radius == 132
    assert 1200 - math.hypot(box[1], box[3]) == pytest.approx(-310.379, abs=.01)


def test_capsule_error_clamps_at_endcaps():
    points = np.array([[-3, 0], [5, 2], [12, 1]])
    assert study.capsule_error(points, np.array([0, 0]), np.array([10, 0])) == 3


def test_drift_ellipse_covers_all_signed_range_corners():
    heading = study.drift_frame(np.array([3, 4]))
    extents = [[-1, 99.89], [-4.613, 21.012]]
    anchor = np.array([4, -6])
    box, radius = study.ellipse(anchor, heading, extents)
    for f in extents[0]:
        for c in extents[1]:
            n, e = anchor + np.array([f, c]) @ heading
            assert box[0] <= n <= box[1] and box[2] <= e <= box[3]
            assert math.hypot(n, e) <= radius
    with pytest.raises(ValueError, match='missing transition heading'):
        study.drift_frame(np.zeros(2))


def test_phase_trace_keeps_only_real_samples_and_brackets_endpoints():
    points = np.array([[0, 0, 0, 70], [8, 8, -8, 78], [16, 16, -16, 86]])
    raw, trace = study.phase_trace(points, 2, 12)
    assert raw[:, 0].tolist() == [8]
    assert trace[:, 0].tolist() == [2, 8, 12]
    assert trace[-1].tolist() == [12, 12, -12, 82]
    with pytest.raises(ValueError, match='window not bracketed'):
        study.phase_trace(points, 2, 20)


def test_missing_ulog_raises():
    with pytest.raises(ValueError, match='invalid ULog header'):
        study.read_positions(b'', 0, 1)
