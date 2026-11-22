from __future__ import annotations

import json

import pytest

from scripts.realworld.horizon_report import main, summarize


def row(error, *, mode="lock", fresh=True, feasible=True, degraded=False, yaw_degraded=False):
    return {
        "flight_stage": "FW",
        "gimbal_state": {
            "fresh": fresh,
            "stabilization": {
                "mode": mode,
                "horizon_error_deg": error,
                "horizon_feasible": feasible,
                "horizon_degraded": degraded,
                "yaw_degraded": yaw_degraded,
                "yaw_error_deg": -30 if yaw_degraded else 0,
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
    path.write_text(json.dumps(row(0, feasible=None)))
    result = summarize(path)
    assert result["feasibility_unknown_frames"] == 1
    assert not result["recommended_passed"]
    assert not result["passed"]


def test_horizon_acceptance_checks_only_reachable_errors_and_nonyaw_degradation(tmp_path):
    path = tmp_path / "manifest.jsonl"
    # 40 reachable frames. The worst reachable frame is exactly 8°, P95 is
    # 4°, and exactly 2/40 have real horizon degradation. Yaw can be relaxed
    # on every reachable frame without failing its separate metric.
    frames = [row(4, degraded=i < 2, yaw_degraded=True) for i in range(39)]
    frames += [row(8, yaw_degraded=True), row(100, feasible=False, degraded=True),
               row(150, feasible=None, degraded=True), row(160, feasible="unknown", degraded=True)]
    path.write_text("\n".join(map(json.dumps, frames)))
    report = summarize(path)
    horizon = report["horizon_acceptance"]
    assert report["passed"] is True and report["legacy_strict_passed"] is False
    assert horizon["feasible_error"]["p95_deg"] == 4
    assert horizon["feasible_error"]["max_deg"] == 8
    assert horizon["degraded_fraction_of_feasible"] == 0.05
    assert horizon["yaw_degraded_fraction"] == 40 / 43
    assert horizon["yaw_degraded_absolute_error"]["max_deg"] == 30
    assert horizon["infeasible_frames"] == 1 and horizon["unknown_frames"] == 2
    assert report["modes"]["lock"]["max_deg"] == 160


@pytest.mark.parametrize(
    "edits,reason",
    [
        ({0: row(8.01)}, "horizon_feasible_max_exceeded"),
        ({i: row(5) for i in range(5)}, "horizon_feasible_p95_exceeded"),
        ({i: row(1, degraded=True) for i in range(5)}, "horizon_degraded_fraction_exceeded"),
        ({i: row(1, feasible=None if i % 2 == 0 else False) for i in range(13)},
         "horizon_unavailable_fraction_exceeded"),
    ],
)
def test_each_horizon_gate_can_fail_on_its_own(tmp_path, edits, reason):
    path = tmp_path / "manifest.jsonl"
    frames = [edits.get(i, row(1)) for i in range(80)]
    path.write_text("\n".join(map(json.dumps, frames)))
    horizon = summarize(path)["horizon_acceptance"]
    assert horizon["passed"] is False
    assert horizon["errors"] == [reason]


@pytest.mark.parametrize("missing_key", ["horizon_feasible", "horizon_degraded", "yaw_degraded", "yaw_error_deg"])
def test_missing_horizon_acceptance_data_rejects_a_round(tmp_path, missing_key):
    path = tmp_path / "manifest.jsonl"
    missing = row(1, yaw_degraded=True)
    del missing["gimbal_state"]["stabilization"][missing_key]
    path.write_text("\n".join(map(json.dumps, [row(1) for _ in range(19)] + [missing])))
    horizon = summarize(path)["horizon_acceptance"]
    assert horizon["passed"] is False
    assert horizon["errors"] == ["horizon_data_missing"]
    assert horizon["missing_frames"] == 1


def test_exactly_fifteen_percent_unavailable_is_accepted(tmp_path):
    path = tmp_path / "manifest.jsonl"
    frames = [row(1) for _ in range(17)] + [row(100, feasible=False), row(110, feasible=None),
                                            row(120, feasible=False)]
    path.write_text("\n".join(map(json.dumps, frames)))
    horizon = summarize(path)["horizon_acceptance"]
    assert horizon["passed"]
    assert horizon["unavailable_fraction"] == 0.15


def track_row(pointing, *, reachable=True, horizon=80, fresh=True):
    result = row(horizon, mode="track", fresh=fresh)
    result["gimbal_state"]["stabilization"] = {
        "mode": "track", "target_ref": {"kind": "world_point", "lat_deg": 42,
                                          "lon_deg": -93, "alt_m": 300},
        "pointing_error_deg": pointing, "target_reachable": reachable,
        "horizon_error_deg": horizon, "horizon_feasible_given_target": False,
        "measurement_fresh": fresh,
    }
    return result


def test_track_acceptance_has_separate_reachable_pointing_and_horizon_statistics(tmp_path):
    path = tmp_path / "manifest.jsonl"
    # P95 exactly 2°, max exactly 5°, plus unreachable target errors that must
    # remain visible without polluting the reachable pointing threshold.
    frames = [track_row(2) for _ in range(39)] + [track_row(5)]
    frames += [track_row(140, reachable=False)]
    path.write_text("\n".join(map(json.dumps, frames)))
    report = summarize(path, expected_mode="track")
    track = report["track_acceptance"]
    assert report["passed"] and track["passed"]
    assert track["reachable_pointing_error"]["p95_deg"] == 2
    assert track["reachable_pointing_error"]["max_deg"] == 5
    assert track["unreachable_frames"] == 1
    assert track["unreachable_pointing_error"]["max_deg"] == 140
    assert track["horizon_error"]["max_deg"] == 80
    assert track["horizon_infeasible_given_target_frames"] == 41
    assert report["horizon_acceptance"]["frame_count"] == 0
    assert not report["horizon_acceptance"]["passed"]
    assert summarize(path)["passed"]
    assert main([str(path), "--expected-mode", "track"]) == 0


@pytest.mark.parametrize("frames,reason", [
    ([track_row(5.01)] + [track_row(1) for _ in range(20)], "track_reachable_max_exceeded"),
    ([track_row(2.01)], "track_reachable_p95_exceeded"),
    ([track_row(0, fresh=False)], "track_measurement_stale"),
    ([track_row(0, reachable=None)], "track_data_missing"),
    ([track_row(100, reachable=False)], "track_no_reachable_frames"),
    ([track_row(float("nan"))], "track_data_missing"),
    ([track_row(-1)], "track_data_missing"),
    ([track_row(0), {"event": "truncated"}], "track_manifest_truncated"),
])
def test_track_acceptance_rejects_bad_pointing_and_incomplete_evidence(tmp_path, frames, reason):
    path = tmp_path / "manifest.jsonl"
    path.write_text("\n".join(map(json.dumps, frames)))
    report = summarize(path, expected_mode="track")
    assert not report["passed"]
    assert reason in report["track_acceptance"]["errors"]


@pytest.mark.parametrize("field", ["target_ref", "pointing_error_deg", "target_reachable",
                                  "horizon_error_deg", "horizon_feasible_given_target"])
def test_track_requires_per_frame_schema(tmp_path, field):
    frame = track_row(1)
    del frame["gimbal_state"]["stabilization"][field]
    path = tmp_path / "manifest.jsonl"
    path.write_text(json.dumps(frame))
    assert "track_data_missing" in summarize(path)["track_acceptance"]["errors"]


def test_vertical_track_can_report_undefined_horizon_without_rejection(tmp_path):
    frame = track_row(0.1, horizon=None)
    frame["gimbal_state"]["stabilization"]["horizon_feasible_given_target"] = None
    path = tmp_path / "manifest.jsonl"
    path.write_text(json.dumps(frame))
    result = summarize(path)
    assert result["passed"]
    assert result["track_acceptance"]["horizon_error"]["count"] == 0
    assert result["track_acceptance"]["horizon_unknown_given_target_frames"] == 1


def test_mixed_track_and_lock_use_independent_gates_and_explicit_expected_mode(tmp_path):
    path = tmp_path / "manifest.jsonl"
    path.write_text("\n".join(map(json.dumps, [row(1), track_row(0.1)])))
    result = summarize(path)
    assert result["passed"]
    assert result["horizon_acceptance"]["frame_count"] == 1
    assert result["track_acceptance"]["frame_count"] == 1
    assert "track_unexpected_mode_frames" in summarize(path, expected_mode="track")["track_acceptance"]["errors"]
    assert "horizon_unexpected_track_frames" in summarize(path, expected_mode="lock")["horizon_acceptance"]["errors"]
    path.write_text("\n".join(map(json.dumps, [row(9), track_row(0.1)])))
    assert not summarize(path)["passed"]
    path.write_text("\n".join(map(json.dumps, [row(1), track_row(6)])))
    assert not summarize(path)["passed"]


def test_track_iowa_coverage_is_separate_from_lock(tmp_path):
    path = tmp_path / "manifest.jsonl"
    frames = [{**track_row(0.1), "flight_state": {"vtol_state": state},
               "attitude": {"roll_deg": 35, "pitch_deg": -85}}
              for state in (1, 2, 3, 4) for _ in range(2)]
    path.write_text("\n".join(map(json.dumps, frames)))
    assert summarize(path, require_iowa_coverage=True, expected_mode="track")["passed"]
    path.write_text("\n".join(map(json.dumps, frames[:-1])))
    assert "track_iowa_coverage_missing" in summarize(
        path, require_iowa_coverage=True, expected_mode="track")["track_acceptance"]["errors"]
