import gzip
import json
from pathlib import Path

import pytest

from scripts.realworld.x1_gimbal_eval import evaluate, expected_grid, main, markdown

IDENTITY = [1, 0, 0, 0, 1, 0, 0, 0, 1]
CAMERA = {"fx": 100, "fy": 100, "cx": 50, "cy": 40, "width": 100, "height": 80}


def write(path, *documents):
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "wt", encoding="utf-8") as stream:
        for document in documents:
            stream.write(json.dumps(document) + "\n")
    return path


def lock(angle, *, feasible=True, degraded=False):
    return {"gimbal_state": {"fresh": True, "stabilization": {
        "mode": "lock", "measurement_fresh": True, "horizon_feasible": feasible,
        "horizon_degraded": degraded, "yaw_degraded": False, "horizon_error_deg": angle,
    }}}


def track(direction=(1, 0, 0), *, fresh=True, angle=None, camera=CAMERA):
    return {"gimbal_state": {"fresh": fresh, "stabilization": {
        "mode": "track", "measurement_fresh": fresh, "target_reachable": True,
        "horizon_feasible_given_target": True, "horizon_error_deg": 6 if fresh else 150,
        "pointing_error_deg": angle, "target_ref": {"kind": "direction_ned", "vector": direction},
    }}, "camera_pose": {"age_ms": 33, "optical_to_world": IDENTITY}, "camera_info": camera}


def episode(batch, job, frames, *, condition=None, accepted=True, recorded_gate=True):
    attempt = batch / "jobs" / job / "attempt-1"
    ep = attempt / "flight/episodes/episode-0000"
    meta = {"status": "complete", "simulator_container": {"container_id": job}}
    write(ep / "episode.json", meta)
    write(ep / "video/manifest.jsonl.gz", *frames)
    site, time, weather = condition or ("rw_farm_iowa", "noon", "clear")
    receipt = {"attempt_id": f"{job}/attempt-1", "attempt_dir": "/old/mount/unused",
               "container_id": job, "accepted": accepted, "valid_round": True,
               "route_completed": True, "max_distance_from_spawn_m": 4150,
               "rtf_window_median": .98, "rtf_delta_sim_over_delta_real": .94,
               "inputs": {"site": site, "time_of_day": time, "weather": weather}}
    if recorded_gate is not None:
        receipt["horizon_acceptance"] = {"passed": recorded_gate}
    write(attempt / "batch-result.json", receipt)
    return ep, receipt


def iowa(report):
    return next(c for c in report["conditions"] if c["site"] == "rw_farm_iowa"
                and c["time_of_day"] == "noon" and c["weather"] == "clear")


def test_grid_is_complete_and_includes_only_wide_area_conditions():
    grid = expected_grid()
    assert len(grid) == len(set(grid)) == 21
    assert ("rw_farm_iowa", "noon", "clear") in grid
    assert ("rw_city_zurich", "dusk", "clear") in grid
    assert not any("grindelwald" in site for site, _, _ in grid)
    report = evaluate()
    assert report["condition_count"] == 21
    assert report["covered_condition_count"] == 0
    assert iowa(report)["horizon"] is None
    assert iowa(report)["rtf_window_median"]["p50"] is None


def test_accepted_ledger_admission_does_not_hide_regressions_or_mix_legacy(tmp_path):
    batch = tmp_path / "nested/batch"
    modern, r1 = episode(batch, "modern", [lock(1), lock(9, degraded=True)])
    _, r2 = episode(batch, "another", [lock(3), lock(40, feasible=False)])
    historical, old = episode(batch, "historical", [lock(1, feasible=False)], recorded_gate=None)
    unlisted, _ = episode(batch, "unlisted", [lock(2)], recorded_gate=None)
    rejected, _ = episode(batch, "rejected", [lock(90)], accepted=False, recorded_gate=False)
    wrong_source = write(tmp_path / "results.jsonl", r1)
    write(batch / "accepted.jsonl.gz", r1, r2, old)
    report = evaluate([modern, unlisted, rejected], accepted_roots=[tmp_path, wrong_source])
    assert any("--accepted-root file must be accepted.jsonl" in issue for issue in report["issues"])
    assert report["accepted_grid_episode_count"] == 2
    assert report["covered_condition_count"] == 1
    cell = iowa(report)
    assert cell["horizon_gate_passed_count"] == 0
    horizon = cell["horizon"]
    assert horizon["feasible_error_deg"] == {
        "count": 3, "p50": 3, "p95": pytest.approx(8.4), "max": 9,
    }
    assert horizon["degraded_frames"] == horizon["unavailable_frames"] == 1
    assert horizon["degraded_fraction_of_feasible"] == pytest.approx(1 / 3)
    assert horizon["unavailable_fraction"] == .25
    rows = {Path(r["episode_dir"]): r for r in report["episodes"]}
    assert rows[modern]["recorded_horizon_acceptance_passed"] is True
    assert rows[modern]["horizon_acceptance"]["passed"] is False
    for old_ep in (historical, unlisted):
        row = rows[old_ep]
        assert row["accepted"] is True
        assert row["recorded_horizon_acceptance_passed"] is None
        assert not row["in_accepted_grid"]
        assert row["flight"]["route_completed"] is True
        assert row["flight"]["rtf_window_median"] == .98
    assert rows[rejected]["accepted"] is False
    assert not rows[rejected]["in_accepted_grid"]
    assert rows[rejected]["flight"]["rtf_window_median"] == .98
    assert "3.000 / 8.400 / 9" in markdown(report)
    assert "1/3 (33.3%)" in markdown(report)


def test_wrong_container_cannot_borrow_accepted_receipt_or_rtf(tmp_path):
    ep, row = episode(tmp_path, "job", [lock(1)])
    write(ep / "episode.json", {"simulator_container": {"container_id": "different"}})
    write(tmp_path / "accepted.jsonl", row)
    report = evaluate(accepted_roots=[tmp_path])
    assert report["accepted_grid_episode_count"] == 0
    row = report["episodes"][0]
    assert row["receipt"] is row["accepted"] is None
    assert row["flight"]["rtf_window_median"] is None


def test_track_reports_fresh_angular_samples_and_geometric_bounds(tmp_path):
    world = track(angle=0)
    world["gimbal_state"]["stabilization"]["target_ref"] = {
        "kind": "world_point", "lat_deg": .001, "lon_deg": 0, "alt_m": 0,
        "altitude_reference": "AMSL",
    }
    world["pose"] = {"lat_deg": 0, "lon_deg": 0, "alt_amsl_m": 0,
                     "global_position_skew_ms": -80, "local_ned_m": [1, 20, 0]}
    world["camera_pose"]["center_ned"] = [1, 21, 0]
    frames = [lock(2), world, track((1, 0, 1), angle=45), track((-1, 0, 0), angle=180),
              track(fresh=False, angle=170), track(angle=0, camera={}), {"event": "truncated"}]
    ep, receipt = episode(tmp_path, "track", frames)
    write(tmp_path / "accepted.jsonl", receipt)
    report = evaluate([ep], accepted_roots=[tmp_path])
    row = report["episodes"][0]
    t = row["tracking"]
    assert row["frame_count"] == 6 and row["truncated"] is True
    assert row["horizon_acceptance"]["frame_count"] == 1
    assert row["horizon_acceptance"]["feasible_error"]["p50_deg"] == 2
    assert row["track_acceptance"]["frame_count"] == 5
    assert t["active_frames"] == 5 and t["fresh_frames"] == 4
    assert (t["in_view_frames"], t["out_of_view_frames"], t["unresolved_frames"]) == (1, 2, 2)
    assert t["pointing_error_deg"] == {"count": 4, "p50": 22.5, "p95": pytest.approx(159.75),
                                      "max": 180}
    assert t["track_horizon_error_deg"]["max"] == 6
    assert t["geometric_in_view_fraction_of_resolved"] == pytest.approx(1 / 3)
    assert t["geometric_in_view_fraction_bounds"] == {"lower": .2, "upper": .6}
    assert iowa(report)["tracking"] == t
    text = markdown(report)
    assert "20.0% to 60.0%" in text
    assert "not visual lock" in text


def test_raw_ack_evidence_is_not_stream_rejection_count(tmp_path):
    ep, _ = episode(tmp_path, "gimbal", [])
    write(ep / "mavlink.jsonl.gz",
          {"message_type": "host_horizon_dispatch", "sequence": 1},
          {"message_type": "GIMBAL_DEVICE_SET_ATTITUDE"},
          {"mavpackettype": "COMMAND_ACK", "command": 1000, "result": 2,
           "_source_system": 1, "_source_component": 154, "receive_monotonic_ns": 40},
          {"message_type": "COMMAND_ACK", "command": 1001, "result": 5},
          {"message_type": "COMMAND_ACK", "command": 1000, "result": 0},
          {"message_type": "COMMAND_ACK", "command": 178, "result": 4},
          {"message_type": "COMMAND_ACK", "command": 1000},
          {"message_type": "command_result", "command": 1000, "result": 2})
    ack = evaluate([ep])["episodes"][0]["mavlink"]
    assert ack["command_ack_rows"] == 5
    assert ack["rejected_ack_rows_observed"] == ack["unidentifiable_ack_rows"] == 1
    assert ack["host_stream_dispatch_rows"] == 1
    assert ack["observed_ackless_messages"] == [
        "GIMBAL_DEVICE_SET_ATTITUDE", "GIMBAL_MANAGER_SET_ATTITUDE",
    ]
    assert ack["rejection_evidence"] == [{
        "line": 3, "command": 1000, "result": 2, "command_name": "DO_GIMBAL_MANAGER_PITCHYAW",
        "result_name": "DENIED", "_source_system": 1, "_source_component": 154,
        "receive_monotonic_ns": 40,
    }]


def test_evidence_cli_keeps_partner_metrics_and_missing_data_distinct(tmp_path):
    ep = tmp_path / "flight/episodes/episode0"
    meta = {"simulator_container": {"container_id": "new"}}
    write(ep / "episode.json", meta)
    write(ep / "video/manifest.jsonl", track(angle=0))
    write(tmp_path / "flight/long-range-result.json", {
        "episode": str(ep), "route_completed": False, "max_distance_from_spawn_m": 12,
    })
    write(tmp_path / "stats.json", {
        "measurement_complete": True, "errors": [], "container_id": "new", "rtf_window_median": 1.0,
    })
    evidence = write(tmp_path / "evidence.json", {"episodes": [{
        "episode_dir": str(ep.relative_to(tmp_path)), "evidence_label": "diagnostic",
    }, {"episode_dir": "not-retained"}]})
    out_json, out_markdown = tmp_path / "out/report.json", tmp_path / "out/REPORT.md"
    assert main(["--evidence", str(evidence), "--json-out", str(out_json),
                 "--markdown-out", str(out_markdown)]) == 0
    report = json.loads(out_json.read_text())
    row, absent = report["episodes"]
    assert row["evidence_label"] == "diagnostic"
    assert row["flight"]["route_completed"] is False
    assert row["flight"]["max_distance_from_spawn_m"] == 12
    assert row["flight"]["rtf_window_median"] == 1.0
    assert row["flight"]["rtf_delta_sim_over_delta_real"] is None
    assert row["mavlink"]["rejected_ack_rows_observed"] is None
    assert absent["tracking"] is absent["frame_count"] is None
    assert "diagnostic only" in out_markdown.read_text()


def test_tracking_hold_splits_frames_and_negative_ack_observations(tmp_path, monkeypatch):
    # The fixture's marker times encode a 5 s split; the production window is longer.
    import scripts.realworld.x1_gimbal_eval as evaluator_module
    monkeypatch.setattr(evaluator_module, "X1_TRACK_TRANSIENT_SIM_S", 5)
    ep, _ = episode(tmp_path, "dense", [lock(2), track(angle=7)])
    write(ep / "episode.json", {"horizon_stabilization": {"eval_track_step": 24,
                                                         "eval_tracking_sample_hz": 10}})
    ns = 10**9

    def marker(message, sim_s, wall_s):
        return {"message_type": f"collector_gimbal_eval_{message}", "step_index": 24,
                "sim_stamp_ns": int(sim_s * ns), "marker_monotonic_ns": int(wall_s * ns)}

    def sample(sim_s, rotation, *, age=0, step=24):
        return {"message_type": "collector_gimbal_eval_sample", "step_index": step,
                "sim_stamp_ns": int(sim_s * ns), "attitude_skew_ms": age, "sim_skew_ms": 0,
                "gimbal_state": {"fresh": True, "source": "gazebo_joint_state"},
                "camera_pose": {"age_ms": 0, "optical_to_world": rotation}, "camera_info": CAMERA}

    def ack(wall_s, *, result=2, command=1000):
        return {"message_type": "COMMAND_ACK", "command": command, "result": result,
                "receive_monotonic_ns": int(wall_s * ns)}

    import math
    from multidetect.camera_mount import rpy_rotation

    rolled = list(rpy_rotation(roll_rad=math.radians(15), pitch_rad=0., yaw_rad=0.))
    away = list(rpy_rotation(roll_rad=0., pitch_rad=0., yaw_rad=math.pi / 2))
    engage = {**marker("mode", 10, 20), "mode": "track",
              "target_ref": {"kind": "direction_ned", "vector": [1, 0, 0]}}
    rows = [engage, sample(9, away), sample(10, away), sample(12, rolled), sample(12, away),
            sample(14, IDENTITY, age=201), sample(14, away, step=25), sample(15, IDENTITY),
            sample(20, IDENTITY), sample(70, away), marker("steady", 15.05, 26), marker("end", 70, 91),
            ack(19), ack(20), ack(26), ack(28, result=0), ack(29, command=178), ack(91),
            {"message_type": "host_horizon_dispatch"}]
    path = write(ep / "mavlink.jsonl.gz", *rows)
    report = evaluate([ep])
    row = report["episodes"][0]
    hold = row["track_diagnostics"]["hold"]
    assert row["horizon_acceptance"]["frame_count"] == row["tracking"]["active_frames"] == 1
    assert hold["sample_count"] == 5 and hold["duration_sim_s"] == 60
    transient, steady = hold["transient"], hold["steady"]
    assert transient["duration_sim_s"] == 5 and steady["duration_sim_s"] == 55
    assert transient["pointing_error_deg"] == {"count": 2, "p50": 45, "p95": 85.5, "max": 90}
    assert steady["pointing_error_deg"]["max"] == 0
    assert hold["overall"]["track_horizon_error_deg"] == {
        "count": 4, "p50": 0, "p95": pytest.approx(12.75), "max": pytest.approx(15),
    }
    # The fixture's overall in-view fraction is far below the 0.9 gate: the run must be flagged.
    assert hold["lock_gate"]["threshold"] == 0.9 and hold["lock_gate"]["passed"] is False
    assert report["lock_gate_failures"] == [row["episode_dir"]]
    assert transient["lock_rate"] == 0.5
    assert transient["lock_rate_bounds"] == {"lower": 1 / 3, "upper": 2 / 3}
    assert steady["lock_rate"] == 1.0
    assert [p["rejected_ack_rows_observed"] for p in (transient, steady)] == [1, 1]
    assert [p["rejection_evidence"][0]["receive_monotonic_ns"] for p in (transient, steady)] == [20 * ns, 26 * ns]
    assert transient["reject_ack_evidence_complete"] is True
    assert "## Tracking hold phases" in markdown(report)

    write(path, *(r for r in rows if r.get("marker_monotonic_ns") != 26 * ns))
    hold_without_ack_split = evaluate([ep])["episodes"][0]["track_diagnostics"]["hold"]
    assert hold_without_ack_split["steady"]["pointing_error_deg"] == steady["pointing_error_deg"]
    assert hold_without_ack_split["steady"]["rejected_ack_rows_observed"] is None

    write(path, *rows, {"message_type": "COMMAND_ACK", "command": 1001, "result": 3})
    hold_partial_ack = evaluate([ep])["episodes"][0]["track_diagnostics"]["hold"]
    assert not hold_partial_ack["steady"]["reject_ack_evidence_complete"]
    assert hold_partial_ack["steady"]["rejected_ack_rows_observed"] == 1
    assert hold_partial_ack["rejected_ack_rows_without_receive_stamp"] == 1


def test_hover_preaim_not_ready_marker_fails_offline_evaluation(tmp_path, capsys):
    ep, _ = episode(tmp_path, "preaim-timeout", [lock(1)])
    failure = {
        "message_type": "collector_gimbal_eval_hover_not_ready",
        "step_index": 7,
        "consecutive_samples": 2,
        "required_consecutive_samples": 5,
        "last_yaw_deg": -359.5,
        "last_failure_reasons": ["joint_yaw_outside_margin"],
    }
    write(ep / "mavlink.jsonl", failure)

    report = evaluate([ep])
    assert report["hover_preaim_failures"] == [{"episode_dir": str(ep), "markers": [failure]}]
    assert report["episodes"][0]["track_diagnostics"]["hold"]["status"] == (
        "hover_preaim_not_ready"
    )
    assert main([str(ep)]) == 5
    assert "hover pre-aim readiness check(s) failed" in capsys.readouterr().err
