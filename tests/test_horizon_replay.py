"""Replay the read-only real Iowa episode when mounted; no simulator is started."""
import json
import logging
import math
import os
from pathlib import Path

import pytest

from scripts.realworld.horizon_replay import load_episode, replay
from multidetect.camera_mount import _mat_mul
from multidetect.sim_teacher.horizon_stabilization import (
    AXES, BRIDGE_PREVIEW_S, preview_body_rotation, rotation_deg,
)
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
    assert result["horizon_feasible_model"]["count"] == 113
    assert result["horizon_feasible_model"]["p95_deg"] <= 4
    assert result["horizon_feasible_model"]["max_deg"] <= 8
    assert result["horizon_degraded_frames"] <= 0.05 * 113
    assert result["yaw_degraded_frames"] == 32
    assert {f["frame_index"] for f in result["frames"] if f["yaw_degraded"]} == {
        f["frame_index"] for f in result["frames"] if not f["target_pose_feasible"]
    }
    assert result["feasible_model"]["count"] == 81
    assert result["feasible_model"]["p95_deg"] <= 4
    assert result["frames"][62]["model_error_deg"] < 1
    report = summarize(EPISODE, require_iowa_coverage=True)
    assert report["mechanical_infeasible_frames"] == 32
    assert report["saturated_frames"] == 27
    assert report["feasible_lock"]["p95_deg"] == pytest.approx(3.2396477617)
    assert not report["passed"] and not report["recommended_passed"]


def test_bridge_preview_right_integrates_frd_and_bounds_prediction():
    body = rotation_deg(dict(roll=30, pitch=-90, yaw=110))
    expected = _mat_mul(body, rotation_deg(dict(roll=0, pitch=12, yaw=0)))
    assert preview_body_rotation(body, (0.0, 20.0, 0.0), BRIDGE_PREVIEW_S) == pytest.approx(expected)
    assert preview_body_rotation(body, (0.0, math.nan, 1.0), BRIDGE_PREVIEW_S) == body
    assert preview_body_rotation(body, (0.0, 0.0, 0.0), BRIDGE_PREVIEW_S) == body


def test_bridge_preview_on_recorded_iowa_turn_exit():
    fixture = Path(__file__).parent / "fixtures/iowa_c11_turn_exit_303.json"
    data = json.loads(fixture.read_text())
    attitudes = [(row[0], dict(zip(AXES, row[1:4], strict=True)), tuple(row[4:]))
                 for row in data["attitudes"]]
    kwargs = dict(bridge_model=True, actuator_delay_s=.25)
    before = replay(data["frames"], attitudes, **kwargs)
    after = replay(data["frames"], attitudes, preview_s=BRIDGE_PREVIEW_S, **kwargs)
    assert before["all_model"]["max_deg"] == pytest.approx(12.051, abs=.001)
    assert after["all_model"]["max_deg"] < 3
    assert after["max_step_deg"] <= 3.6 + 1e-7
    assert after["measured"] == before["measured"]
    assert after["measured"]["max_deg"] == pytest.approx(12.218546)
    assert after["measured_fk_max_matrix_difference"] < 1e-12
