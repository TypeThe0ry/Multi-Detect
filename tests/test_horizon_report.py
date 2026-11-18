from __future__ import annotations

import json

import pytest

from scripts.realworld.horizon_report import main, summarize


def row(error, *, mode="lock", fresh=True):
    return {
        "flight_stage": "FW",
        "gimbal_state": {
            "fresh": fresh,
            "stabilization": {
                "mode": mode,
                "horizon_error_deg": error,
                "measurement_fresh": fresh,
                "control": {"degraded": False},
            },
        },
    }


def test_report_counts_all_recorded_frames_and_threshold_failures(tmp_path):
    manifest = tmp_path / "video" / "manifest.jsonl"
    manifest.parent.mkdir()
    manifest.write_text("\n".join(map(json.dumps, [row(1), row(3)])))
    result = summarize(tmp_path)
    assert result["frame_count"] == 2
    assert result["modes"]["lock"]["p50_deg"] == 2
    assert result["modes"]["lock"]["p95_deg"] == pytest.approx(2.9)
    assert result["passed"] is True
    assert summarize(tmp_path, threshold_deg=2)["passed"] is False
    assert main([str(tmp_path), "--threshold-deg", "2"]) == 1
    with manifest.open("a") as stream:
        stream.write('\n{"event": "truncated"}\n')
    result = summarize(tmp_path)
    assert result["truncated"] is True
    assert result["passed"] is False


@pytest.mark.parametrize(
    "rows",
    [[row(1, fresh=False)], [row(None)], [{}], [], [row(0, mode="follow")], [row(float("nan"))]],
)
def test_report_does_not_accept_stale_missing_empty_or_follow_only(tmp_path, rows):
    path = tmp_path / "manifest.jsonl"
    path.write_text("\n".join(map(json.dumps, rows)))
    assert summarize(path)["passed"] is False


def test_iowa_coverage_requires_measured_frames_in_each_flight_regime(tmp_path):
    path = tmp_path / "manifest.jsonl"
    rows = [
        {**row(2), "flight_state": {"vtol_state": vtol_state},
         "attitude": {"roll_deg": 35, "pitch_deg": -85}}
        for vtol_state in (1, 2, 3, 4)
        for _ in range(2)
    ]
    path.write_text("\n".join(map(json.dumps, rows)))
    result = summarize(path, require_iowa_coverage=True)
    assert result["passed"]
    assert result["vertical_mc_frames"] == 2
    rows[-1] = row(None, fresh=False)
    path.write_text("\n".join(map(json.dumps, rows)))
    result = summarize(path, require_iowa_coverage=True)
    assert not result["passed"]
    assert result["missing_frames"] == result["stale_frames"] == 1
    assert not result["iowa_coverage_passed"]


def classified_row(error, roll=0):
    result = row(error)
    result["attitude"] = {"roll_deg": roll, "pitch_deg": -54, "yaw_deg": 0}
    state = result["gimbal_state"]
    state["joint_position_rad"] = {"roll": -0.785398 if roll else 0, "pitch": 0, "yaw": 0}
    state["stabilization"]["desired_earth_deg"] = {"roll": 0, "pitch": -45, "yaw": 0}
    return result


def test_proposed_feasible_gate_keeps_strict_gate_and_dynamic_degradation(tmp_path):
    path = tmp_path / "manifest.jsonl"
    rows = [classified_row(1) for _ in range(20)] + [classified_row(100, roll=80)]
    path.write_text("\n".join(map(json.dumps, rows)))
    result = summarize(path)
    assert not result["passed"]
    assert result["recommended_passed"]
    assert result["feasible_lock"]["count"] == 20
    assert result["infeasible_lock"]["count"] == 1
    assert result["saturated_frames"] == 1
    assert result["horizon_only_proven_feasible_frames"] == 21
    for entry in rows[:20]:
        entry["gimbal_state"]["stabilization"]["horizon_error_deg"] = 8
        entry["gimbal_state"]["stabilization"]["control"] = {"degraded": True}
    path.write_text("\n".join(map(json.dumps, rows)))
    assert not summarize(path)["recommended_passed"]


def test_unknown_feasibility_cannot_silently_pass_proposed_gate(tmp_path):
    path = tmp_path / "manifest.jsonl"
    path.write_text(json.dumps(row(0)))
    result = summarize(path)
    assert result["feasibility_unknown_frames"] == 1
    assert not result["recommended_passed"]
