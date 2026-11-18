"""Replay the read-only real Iowa episode when mounted; no simulator is started."""
import logging
import os
from pathlib import Path

import pytest

from scripts.realworld.horizon_replay import load_episode, replay
from scripts.realworld.horizon_report import summarize

EPISODE = Path(os.environ.get("IOWA_GIMBAL_EPISODE", "/data/datasets/multidetect-sim-assets/"
    "wide-collect/batches/x1-iowa-gimbal/jobs/"
    "2052f618de1ead05a0ab8d52dd0c8690504b127f8ef9564afacd520be08e559e/"
    "attempt-1/flight/episodes/episode-0000-0d180946"))


@pytest.mark.optional
def test_real_episode_attitudes_and_measured_joints(caplog):
    if not EPISODE.is_dir():
        pytest.skip("read-only Iowa episode not mounted; set IOWA_GIMBAL_EPISODE")
    caplog.set_level(logging.ERROR)
    frames, attitudes = load_episode(EPISODE)
    assert len(frames) == 113 and len(attitudes) == 39721
    result = replay(frames, attitudes)
    assert result["control_steps"] == 33646
    assert result["max_step_deg"] <= 3.6 + 1e-7
    assert result["measured_fk_max_matrix_difference"] < 1e-12
    assert result["measured"]["max_deg"] == pytest.approx(158.776199)
    assert result["all_model"]["max_deg"] < 40
    assert result["feasible_model"]["count"] == 81
    assert result["feasible_model"]["p95_deg"] <= 4
    assert result["frames"][62]["model_error_deg"] < 40
    report = summarize(EPISODE, require_iowa_coverage=True)
    assert report["mechanical_infeasible_frames"] == 32
    assert report["saturated_frames"] == 27
    assert report["feasible_lock"]["p95_deg"] == pytest.approx(3.2396477617)
    assert not report["passed"] and not report["recommended_passed"]
