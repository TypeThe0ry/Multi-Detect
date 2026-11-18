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
