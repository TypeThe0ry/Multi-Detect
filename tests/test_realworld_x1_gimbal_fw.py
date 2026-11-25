"""X1 fixed-wing cruise/turn gimbal tracking evaluation (opt-in, fixture only; no flight)."""

from __future__ import annotations

import json
import math
from dataclasses import replace

import pytest

from scripts import collect_sim_teacher_episodes as collector
from scripts.realworld import long_range_flight as flight, x1_collect_next as queue
from scripts.realworld.collection_runtime import runtime_options
from scripts.realworld.horizon_report import summarize
from scripts.realworld.x1_gimbal_eval import fw_legs, read_track_hold
from multidetect.sim_teacher.gimbal_recording import X1FwPhase

NS = 10**9
IDENTITY = [1, 0, 0, 0, 1, 0, 0, 0, 1]
CAMERA = {"fx": 100, "fy": 100, "cx": 50, "cy": 40, "width": 100, "height": 80}
TARGET_REF = {"kind": "direction_ned", "vector": [1, 0, 0]}
META = {"horizon_stabilization": {"eval_track_step": 5, "eval_fw_legs": 2, "max_age_s": 0.2}}


def tilted(deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return [c, s, 0, -s, c, 0, 0, 0, 1]  # optical axis yawed by ``deg`` from the target direction


def sample(i, phase, error_deg=1.0, *, step=0, mode="track"):
    return {"message_type": "collector_gimbal_eval_sample", "step_index": step, "sim_stamp_ns": i * NS // 10,
            "fw_phase": phase, "track_mode": mode, "attitude_skew_ms": 3, "sim_skew_ms": 4,
            "gimbal_state": {"source": "gazebo_joint_state", "fresh": True},
            "camera_pose": {"age_ms": 5, "optical_to_world": tilted(error_deg), "center_ned": [0, 0, 0]},
            "camera_info": CAMERA, "pose": {"local_ned_m": [0, 0, 0]}}


def events(*rows, with_mode=True):
    mode = [{"message_type": "collector_gimbal_eval_fw_mode", "step_index": 0, "mode": "track",
             "target_ref": TARGET_REF}] if with_mode else []
    return [*mode, *rows]


def good(cruise=150, turn=40, **kw):
    rows = [sample(i, "cruise", **kw) for i in range(cruise)]
    rows += [sample(cruise + i, "turn", 3.0) for i in range(turn)]
    return rows


def test_phase_labels_use_bank_hysteresis_and_fw_state_only():
    phase = X1FwPhase()
    assert phase.update(0.0, 3) is None and phase.update(None, 4) is None  # MC / no roll
    assert [phase.update(r, 4) for r in (2, 14.9, 15, 10, 8.1, 7.9, -20, 0)] == [
        "cruise", "cruise", "turn", "turn", "turn", "cruise", "turn", "cruise"]
    assert phase.update(30, 2) is None and phase.update(10, 4) == "cruise"  # transition resets the latch


def test_fw_eval_is_off_unless_requested():
    assert fw_legs({"horizon_stabilization": {"eval_track_step": 5}}, events(*good())) is None
    assert fw_legs({}, []) is None


def test_fw_eval_scores_cruise_and_turn_separately():
    report = fw_legs(META, events(*good()))
    assert report["status"] == "scored" and report["passed"] is True
    assert report["cruise"]["sample_count"] == 150 and report["turn"]["sample_count"] == 40
    assert report["cruise"]["pointing_error_deg"]["p95"] == pytest.approx(1.0, abs=0.01)
    assert report["turn"]["pointing_error_deg"]["p95"] == pytest.approx(3.0, abs=0.01)
    assert report["cruise"]["lock_rate"] == report["turn"]["lock_rate"] == 1.0
    assert report["unlabeled_samples"] == 0 and report["failed_checks"] == []


def test_fw_gate_fails_on_each_threshold():
    cruise_bad = fw_legs(META, events(*good(error_deg=6.0)))  # cruise P95 limit is 5 deg
    assert cruise_bad["passed"] is False and cruise_bad["failed_checks"] == ["cruise_p95"]
    rows = good()[:150] + [sample(150 + i, "turn", 11.0) for i in range(40)]  # turn limit is 10 deg
    assert fw_legs(META, events(*rows))["failed_checks"] == ["turn_p95"]
    assert fw_legs(META, events(*good(turn=5)))["failed_checks"] == ["turn_samples"]
    assert fw_legs(META, events(*good(cruise=99)))["failed_checks"] == ["cruise_samples"]
    # 60 deg error leaves the +-45/+-35 deg view of the fixture camera: lock rate and P95 both fail.
    out = fw_legs(META, events(*good(error_deg=60.0)))
    assert {"cruise_p95", "cruise_lock_rate"} <= set(out["failed_checks"])
    assert fw_legs(META, events(*good()), )["gate"]["min_lock_rate"] == 0.9


def test_fw_unwound_unlabeled_and_other_steps_are_not_scored():
    rows = good() + [sample(1000 + i, "turn", 50.0, mode="lock") for i in range(10)]  # unwind: lock mode
    rows += [{**sample(2000 + i, None), "fw_phase": None} for i in range(7)]  # transition, no label
    rows += [sample(3000 + i, "cruise", 40.0, step=5) for i in range(50)]  # hover step, not a FW leg
    report = fw_legs(META, events(*rows))
    assert report["unwind_samples"] == 10 and report["unlabeled_samples"] == 7
    assert report["cruise"]["sample_count"] == 150 and report["turn"]["sample_count"] == 40
    assert report["unwind_fraction"] == pytest.approx(10 / 200) and report["passed"] is True
    heavy = fw_legs(META, events(*good(), *[sample(1000 + i, "turn", mode="lock") for i in range(40)]))
    assert heavy["failed_checks"] == ["unwind_fraction"]


def test_fw_samples_are_deduplicated_by_frame_epoch_and_need_a_target():
    rows = good()
    assert fw_legs(META, events(*rows, *rows))["labeled_samples"] == 190
    missing = fw_legs(META, events(*rows, with_mode=False))
    assert missing["status"] == "target_ref_unavailable" and missing["passed"] is False


def test_read_track_hold_attaches_fw_and_markdown_renders(tmp_path):
    ep = tmp_path
    markers = [{"message_type": f"collector_gimbal_eval_{k}", "mode": "track", "step_index": 5,
                "sim_stamp_ns": t, "marker_monotonic_ns": t} for k, t in (("mode", NS), ("steady", 21 * NS),
                                                                             ("end", 61 * NS))]
    with (ep / "mavlink.jsonl").open("w") as log:
        for row in [*markers, *events(*good())]:
            log.write(json.dumps(row) + "\n")
    issues = []
    _, hold = read_track_hold(ep, META, issues)
    assert hold["fw"]["passed"] is True and hold["status"] == "phase_windows_available"
    report = {"episodes": [{"episode_id": "e", "track_diagnostics": {"hold": hold}}], "issues": [],
              "conditions": [], "covered_condition_count": 0, "condition_count": 0,
              "accepted_grid_episode_count": 0}
    # Only the new section is exercised: the generic tables need a full report.
    assert "Fixed-wing cruise/turn tracking" in _fw_markdown(report)


def _fw_markdown(report):
    from scripts.realworld import x1_gimbal_eval as module
    return module.markdown({**report, "episodes": [
        {**e, "in_accepted_grid": False, "evidence_label": None, "condition": None, "frame_count": 0,
         "horizon_acceptance": None, "track_acceptance": None, "tracking": None, "episode_dir": "d",
         "mavlink": {"rejected_ack_rows_observed": None},
         "flight": {"route_completed": None, "max_distance_from_spawn_m": None,
                    "rtf_window_median": None, "rtf_delta_sim_over_delta_real": None},
         "issues": []} for e in report["episodes"]]})


def test_horizon_report_can_exclude_fw_leg_frames(tmp_path):
    def frame(action, mode):
        stab = ({"mode": "track", "pointing_error_deg": 30., "target_reachable": True,
                 "horizon_feasible_given_target": True, "horizon_error_deg": 5.,
                 "target_ref": TARGET_REF} if mode == "track" else
                {"mode": "lock", "horizon_error_deg": 1., "horizon_feasible": True,
                 "horizon_degraded": False, "yaw_degraded": False})
        return {"action_index": action, "sensor_stamp_ns": action,
                "gimbal_state": {"fresh": True, "stabilization": {"measurement_fresh": True, **stab}}}
    path = tmp_path / "manifest.jsonl"
    path.write_text("\n".join(json.dumps(frame(a, m)) for a, m in
                              ((0, "track"), (1, "track"), (3, "lock"), (4, "lock"), (5, "track"))) + "\n")
    mixed = summarize(path, track_action_index=5, exclude_action_below=2)
    assert mixed["track_acceptance"]["frame_count"] == 1
    assert mixed["track_acceptance"]["excluded_fw_leg_frames"] == 2
    assert mixed["horizon_acceptance"]["frame_count"] == 2
    assert summarize(path, track_action_index=5)["track_acceptance"]["frame_count"] == 3


def test_runtime_options_gate_fw_legs_behind_gimbal_eval():
    assert runtime_options({"gimbal_bridge_stabilize": True, "gimbal_eval": True, "gimbal_eval_fw": True},
                           flight.DELTA, "job")["gimbal_eval_fw"] is True
    with pytest.raises(ValueError, match="requires gimbal_eval"):
        runtime_options({"gimbal_bridge_stabilize": True, "gimbal_eval_fw": True}, flight.DELTA, "job")
    with pytest.raises(ValueError, match="JSON boolean"):
        runtime_options({"gimbal_eval_fw": 1}, flight.DELTA, "job")


def test_cpu_plan_with_fw_legs_keeps_route_and_hover(monkeypatch, capsys):
    monkeypatch.delenv("MULTIDETECT_SIM_LISTEN_HOST", raising=False)
    monkeypatch.setattr(collector, "docker_command", lambda *_a, **_kw: pytest.fail("Docker invoked"))
    args = ["7", "--profile", flight.DEFAULT_PROFILE, "--plan-only", "--gimbal-bridge-stabilize",
            "--gimbal-eval"]
    plans = []
    real = flight.gimbal_eval_plan
    monkeypatch.setattr(flight, "gimbal_eval_plan", lambda *a, **k: plans.append(real(*a, **k)) or plans[-1])
    assert flight.main(args) == 0
    hover = json.loads(capsys.readouterr().out)
    assert flight.main([*args, "--gimbal-eval-fw"]) == 0
    fw = json.loads(capsys.readouterr().out)
    legs = len(fw["fw_waypoints_home_ned_m"])
    assert fw["gimbal_eval"]["fw_legs"] == legs == fw["collector_plan"]["gimbal_eval_fw_legs"]
    assert hover["gimbal_eval"]["fw_legs"] == 0
    assert {"fw_switches", "switch_limit_deg"}.isdisjoint(hover["gimbal_eval"])
    assert fw["gimbal_eval"]["switch_limit_deg"] == 30.
    switches = fw["gimbal_eval"]["fw_switches"]
    assert [row["leg"] for row in switches] == list(range(legs))
    assert switches[0]["switch_los_deg"] is switches[0]["switch_ned_m"] is None
    for switch in switches[1:]:
        assert len(switch["switch_ned_m"]) == 3
        assert 0. <= switch["switch_los_deg"] <= 180.
        assert switch["over_limit"] == (switch["switch_los_deg"] > 30.)
    for key in ("steps_per_episode", "gimbal_eval_track_step", "track_targets", "video_hz"):
        assert fw["collector_plan"][key] == hover["collector_plan"][key]
    assert "gimbal_eval_fw_targets" not in hover["collector_plan"]
    assert len(fw["collector_plan"]["gimbal_eval_fw_targets"]) == legs
    assert len(plans[-1].gimbal_eval_fw_targets) == len(set(plans[-1].gimbal_eval_fw_targets))
    assert fw["gimbal_eval"]["track_step"] == legs + 2
    plan = plans[-1]
    assert replace(plan, gimbal_eval_fw_legs=legs).gimbal_eval_fw_legs == legs
    for bad in (-1, plan.gimbal_eval_track_step, 1.0):  # must stay before the hover step, and be an int
        with pytest.raises(ValueError, match="fixed-wing gimbal evaluation"):
            replace(plan, gimbal_eval_fw_legs=bad)
    with pytest.raises(ValueError, match="fixed-wing gimbal evaluation"):
        replace(plan, gimbal_eval_track_step=None, gimbal_eval_fw_legs=1)
    with pytest.raises(SystemExit):
        flight.main(["7", "--profile", flight.DEFAULT_PROFILE, "--plan-only", "--gimbal-bridge-stabilize",
                     "--gimbal-eval-fw"])


def test_queue_config_fw_legs_flag_reaches_the_job(tmp_path, monkeypatch):
    spec = tmp_path / "x1-gimbal-eval.json"
    spec.write_text(json.dumps({"schema_version": 1, "enabled": True, "gimbal_eval": True, "fw_legs": True}))
    monkeypatch.setattr(queue, "EVAL_MANIFEST", spec)
    assert queue.eval_config(require_enabled=True)["fw_legs"] is True
    spec.write_text(json.dumps({"schema_version": 1, "enabled": True, "gimbal_eval": True, "fw_legs": "yes"}))
    with pytest.raises(ValueError, match="fw_legs"):
        queue.eval_config(require_enabled=True)


def test_track_sample_can_carry_the_mavlink_attitude_for_phase_labels():
    from types import SimpleNamespace
    from multidetect.camera_mount import CameraMount
    from multidetect.sim_teacher.gimbal_recording import DeltaCameraGeometry
    from multidetect.sim_teacher.streams import MavlinkState, SensorSample

    stamp = 2_000_000_000
    sensor = SensorSample(b"x", 1, 1, None, {"fx": 100}, {}, [], stamp, 0, 0.,
                          joint_state={"source_timestamp_ns": stamp,
                                       "gimbal_joint_state": {"position_rad": {"roll": 0., "pitch": 0., "yaw": 0.},
                                                              "source": "gazebo_joint_state", "fresh": True}})
    state = MavlinkState(messages={
        "LOCAL_POSITION_NED": {"x": 1, "time_boot_ms": 2000},
        "ATTITUDE": {"roll": math.radians(20), "pitch": 0., "yaw": 0., "time_boot_ms": 2000},
        "GLOBAL_POSITION_INT": {"lat": 1, "lon": 2, "alt": 3000, "time_boot_ms": 2000},
        "GPS_RAW_INT": {"fix_type": 3}})
    reader = SimpleNamespace(aligned_state=lambda *_a, **_k: state)
    geometry = DeltaCameraGeometry((0., 0., 0.), ((0., 0., 0.),) * 3, (0., 0., 0.), "fixture")
    kwargs = dict(camera_mount=CameraMount.gimbal(), camera_geometry=geometry)
    assert "attitude" not in collector._x1_track_sample(reader, sensor, **kwargs)
    row = collector._x1_track_sample(reader, sensor, extra_keys=("attitude",), **kwargs)
    assert row["attitude"]["roll_deg"] == pytest.approx(20.0)
    assert X1FwPhase().update(row["attitude"]["roll_deg"], 4) == "turn"
