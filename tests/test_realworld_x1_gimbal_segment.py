from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scripts import collect_sim_teacher_episodes as collector
from scripts.realworld import long_range_flight as flight, x1_collect_next as queue
from scripts.realworld import wide_collect_batch as batch
from scripts.realworld.collection_runtime import runtime_options
from multidetect.sim_teacher.horizon_stabilization import BridgeHorizonTarget


def test_x1_gimbal_eval_cpu_plan_keeps_flight_route(monkeypatch, capsys):
    monkeypatch.delenv("MULTIDETECT_SIM_LISTEN_HOST", raising=False)
    monkeypatch.setattr(collector, "docker_command", lambda *_a, **_kw: pytest.fail("Docker invoked"))
    args = ["7", "--profile", flight.DEFAULT_PROFILE, "--plan-only", "--gimbal-bridge-stabilize"]
    assert flight.main(args) == 0
    normal = json.loads(capsys.readouterr().out)
    assert flight.main([*args, "--gimbal-eval"]) == 0
    evaluation = json.loads(capsys.readouterr().out)
    for key in ("fw_waypoints_home_ned_m", "compiled_mission", "minimum_rtf", "minimum_max_distance_m"):
        assert normal[key] == evaluation[key]
    plan = evaluation["collector_plan"]
    assert plan["steps_per_episode"] == normal["collector_plan"]["steps_per_episode"] + 1
    assert plan["video_hz"] == normal["collector_plan"]["video_hz"] == pytest.approx(1 / 6)
    assert plan["gimbal_mode"] == "lock"
    assert len(plan["track_targets"]) == 1
    actions = flight.LongRangePolicy(evaluation["fw_waypoints_home_ned_m"], 80, vtol=True, gimbal_eval=True)
    scheduled = [actions.next_action() for _ in range(plan["steps_per_episode"])]
    index = plan["gimbal_eval_track_step"]
    assert [command.kind for command in scheduled[index - 1:]] == ["mc_reposition", "mc_hold", "stop"]
    assert scheduled[index].duration_s == 30.0
    assert scheduled[index].target_ned == scheduled[index - 1].target_ned


def test_x1_gimbal_hover_has_time_to_finish_when_already_at_home(monkeypatch):
    policy = flight.LongRangePolicy([(0., 30., -85.)], 80, vtol=True, gimbal_eval=True)
    hover = [policy.next_action() for _ in range(4)][-1]
    sim = [0.0]
    monkeypatch.setattr(collector, "_SimClock", lambda _: lambda: sim[0])
    monkeypatch.setattr(collector, "time", SimpleNamespace(
        monotonic=lambda: sim[0], sleep=lambda dt: sim.__setitem__(0, sim[0] + dt)))
    monkeypatch.setattr(collector, "_state_position", lambda _: hover.target_ned)
    monkeypatch.setattr(collector, "_state_speed", lambda _: 0.0)
    reader = SimpleNamespace(state=None, raise_if_failed=lambda: None)
    result = collector._wait_motion_command(hover, reader, cruise_mps=15.0)
    assert result["waited"] == "settled" and 30.0 <= result["sim_elapsed_s"] <= 30.2


def test_x1_gimbal_queue_is_disabled_before_any_live_checks(monkeypatch, capsys):
    for method in ("mount_check", "image_check", "flight_gate", "flight_lock"):
        monkeypatch.setattr(queue.resume, method, lambda *_a, **_kw: pytest.fail("runtime check invoked"))
    for argv in (["launch"], ["preflight"], ["run", "--image-id", "sha256:" + "a" * 64]):
        assert queue.main(["--gimbal-eval", *argv]) == 75
        assert "queue is disabled" in capsys.readouterr().err
    with pytest.raises(ValueError, match="requires X1 bridge"):
        runtime_options({"gimbal_eval": True}, flight.DELTA, "job")
    with pytest.raises(ValueError, match="JSON boolean"):
        runtime_options({"gimbal_eval": "true"}, flight.DELTA, "job")


def test_x1_gimbal_bridge_switch_preserves_frame_time_mode():
    target = flight.WorldTarget(42.001, -93, 300)
    bridge = BridgeHorizonTarget({"pitch": -45., "yaw": 0., "roll": 0.}, track_targets=(target,))
    body = {"pitch": 80., "yaw": 10., "roll": 0.}
    lock = bridge.update(body, now_s=1., attitude_time_s=1.)
    bridge.record_command(lock, 1.)
    bridge.set_mode("track")
    track = bridge.update(body, now_s=1.1, attitude_time_s=1.1,
                          position=flight.WorldTarget(42., -93, 385))
    bridge.record_command(track, 1.1)
    bridge.set_mode("lock")
    assert track["target_ref"] == target.reference()
    assert track["transport_flags"] != lock["transport_flags"]
    assert bridge._command_at(1_050_000_000)[0]["mode"] == "lock"
    assert bridge._command_at(1_150_000_000)[0]["mode"] == "track"


def test_x1_gimbal_batch_requires_segment_but_does_not_select_on_pointing(tmp_path):
    attempt = tmp_path
    episode = attempt / "flight/episodes/episode-fixture"
    video = episode / "video"
    (video / "rgb").mkdir(parents=True)
    identity = {"container_id": "x1", "container_name": "md-rw-long-slot-5-fixture"}
    meta = {"status": "complete", "simulator_container": identity,
            "gimbal_check": {"passed": True, "frame_count": 4},
            "horizon_stabilization": {"eval_track_step": 24}}
    (episode / "episode.json").write_text(json.dumps(meta))
    (attempt / "result.json").write_text(json.dumps({
        **identity, "name": "job", "valid_round": True, "route_completed": True, "gpu": 3,
        "airframe": flight.DELTA, "rtf_window_median": 1, "rtf_delta_sim_over_delta_real": 0.95,
        "max_distance_from_spawn_m": 4060, "scenario_class": "wide_area", "minimum_max_distance_m": 4000,
    }))
    manifest = video / "manifest.jsonl"

    def write_frames(lock_error, track_index):
        rows = []
        for idx in range(4):
            stamp = idx * 10**9
            (video / "rgb" / f"{idx}.jpg").write_bytes(b"fixture")
            lock = {"mode": "lock", "horizon_error_deg": lock_error, "horizon_feasible": True,
                    "horizon_degraded": False, "yaw_degraded": False}
            track = {"mode": "track", "pointing_error_deg": 40., "target_reachable": True,
                     "horizon_feasible_given_target": True, "horizon_error_deg": 25.,
                     "target_ref": {"kind": "direction_ned", "vector": [1, 0, 0]}}
            rows.append({"sensor_stamp_ns": stamp, "action_index": track_index if idx else 23,
                         "gimbal_state": {"fresh": True, "reference_frame": "body_FRD", "age_ms": 0,
                                          "source": "gazebo_joint_state", "skew_ms": 0,
                                          "frame_timestamp_ns": stamp, "timestamp_ns": stamp,
                                          "roll_rad": 0, "pitch_rad": 0, "yaw_rad": 0,
                                          "stabilization": {"measurement_fresh": True, **(track if idx else lock)}},
                         "camera_pose": {"center_ned": [0, 0, 0], "age_ms": 0, "reference_frame": "NED",
                                         "extrinsics_source": "fixture", "optical_to_world": [1,0,0,0,1,0,0,0,1]}})
        manifest.write_text("\n".join(map(json.dumps, rows)) + "\n")

    inputs = {"max_artifact_bytes": 8 << 30, "gimbal_bridge_stabilize": True, "gimbal_eval": True}
    job = {"job_id": "job", "inputs": inputs, "matrix": {"airframe": flight.DELTA}, "slot": 5}
    write_frames(1., 24)
    accepted = batch.evaluate(attempt, job, {"gpu": 3}, 0)
    assert accepted["accepted"], accepted
    assert accepted["track_diagnostics"]["passed"] is False
    assert accepted["track_diagnostics"]["eval_action_frames"] == 3
    assert "horizon_unexpected_track_frames" in batch.evaluate(
        attempt, {**job, "inputs": {**inputs, "gimbal_eval": False}}, {"gpu": 3}, 0)["errors"]
    write_frames(10., 25)
    rejected = batch.evaluate(attempt, job, {"gpu": 3}, 0)
    assert "gimbal_eval_track_segment_missing" in rejected["errors"]
    assert "horizon_feasible_p95_exceeded" in rejected["errors"]
