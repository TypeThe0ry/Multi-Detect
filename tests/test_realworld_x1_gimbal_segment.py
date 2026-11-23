from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

from scripts import collect_sim_teacher_episodes as collector
from scripts.realworld import long_range_flight as flight, x1_collect_next as queue
from scripts.realworld import wide_collect_batch as batch
from scripts.realworld.collection_runtime import runtime_options
from multidetect.camera_mount import CameraMount
from multidetect.sim_teacher.gimbal_recording import DeltaCameraGeometry, X1TrackCapture
from multidetect.sim_teacher.horizon_stabilization import BridgeHorizonTarget
from multidetect.sim_teacher.streams import MavlinkState, SensorSample


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
    assert scheduled[index].duration_s == 60.0
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
    assert result["waited"] == "settled" and 60.0 <= result["sim_elapsed_s"] <= 60.2


def test_x1_gimbal_queue_is_disabled_before_any_live_checks(monkeypatch, capsys, tmp_path):
    disabled = tmp_path / "x1-gimbal-eval.json"
    disabled.write_text(json.dumps({"schema_version": 1, "enabled": False, "gimbal_eval": True}))
    monkeypatch.setattr(queue, "EVAL_MANIFEST", disabled)
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


def test_x1_gimbal_capture_samples_hundreds_of_distinct_in_window_frames():
    start = 100_000_000_000
    capture = X1TrackCapture(start)
    requests = []
    reader = SimpleNamespace(latest=SimpleNamespace(sensor_stamp_ns=start - 1), request=requests.append)
    samples = []
    next_frame = start
    for i in range(3000):  # 60 sim seconds, 50 Hz action ticks, 10 Hz source.
        stamp = start + i * 20_000_000
        if requests and stamp >= next_frame:
            reader.latest = SimpleNamespace(sensor_stamp_ns=stamp)
            next_frame = stamp + 100_000_000
        sample = capture.tick(reader, sim_ns=stamp, wall_s=i / 50.0)
        if sample is not None:
            samples.append(sample.sensor_stamp_ns)
    assert 500 <= len(samples) <= 600
    assert all(b - a >= 100_000_000 for a, b in zip(samples, samples[1:]))
    assert start <= samples[0] < samples[-1] < start + 60 * 10**9
    assert X1TrackCapture(None).tick(reader, sim_ns=start, wall_s=0) is None


def test_x1_gimbal_capture_uses_the_measured_joint_sidecar_and_no_rgb():
    stamp = 2_000_000_000
    sample = SensorSample(b"unused rgb", 1, 1, None, {"fx": 100}, {}, [], stamp, 0, 0.,
                          joint_state={"source_timestamp_ns": stamp - 2_000_000,
                                       "gimbal_joint_state": {"position_rad": {"roll": 0.1,
                                                                               "pitch": 0., "yaw": 0.},
                                                              "source": "gazebo_joint_state", "fresh": True}})
    state = MavlinkState(messages={
        "LOCAL_POSITION_NED": {"x": 1, "time_boot_ms": 1990},
        "ATTITUDE": {"roll": 0., "pitch": 0., "yaw": 0., "time_boot_ms": 1995},
        "GLOBAL_POSITION_INT": {"lat": 1, "lon": 2, "alt": 3000, "time_boot_ms": 1998},
        "GPS_RAW_INT": {"fix_type": 3},
    })
    calls = []

    def aligned(sensor_stamp, *, timeout_s):
        calls.append((sensor_stamp, timeout_s))
        return state

    geometry = DeltaCameraGeometry((0., 0., 0.), ((0., 0., 0.),) * 3, (0., 0., 0.), "fixture")
    row = collector._x1_track_sample(SimpleNamespace(aligned_state=aligned), sample,
                                     camera_mount=CameraMount.gimbal(), camera_geometry=geometry)
    assert calls == [(stamp, 0.)]
    assert {"rgb", "depth"}.isdisjoint(row)
    assert row["sim_skew_ms"] == 10 and row["attitude_skew_ms"] == 5
    assert row["gimbal_state"]["source"] == "gazebo_joint_state"
    assert row["gimbal_state"]["joint_position_rad"]["roll"] == 0.1
    assert row["gimbal_state"]["age_ms"] == 2
    assert row["camera_pose"]["age_ms"] == 10
    assert row["camera_pose"]["center_ned"] == [1., 0., 0.]


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
    assert accepted["track_diagnostics"]["hold"]["status"] == "phase_sim_clock_markers_unavailable"
    with (episode / "mavlink.jsonl").open("w") as log:
        for kind, stamp in (("mode", 10**9), ("steady", 6 * 10**9), ("end", 61 * 10**9)):
            log.write(json.dumps({"message_type": f"collector_gimbal_eval_{kind}", "mode": "track",
                                  "step_index": 24, "sim_stamp_ns": stamp,
                                  "marker_monotonic_ns": stamp}) + "\n")
    measured = batch.evaluate(attempt, job, {"gpu": 3}, 0)
    assert measured["accepted"] and measured["track_diagnostics"]["hold"]["sample_count"] == 0
    assert "horizon_unexpected_track_frames" in batch.evaluate(
        attempt, {**job, "inputs": {**inputs, "gimbal_eval": False}}, {"gpu": 3}, 0)["errors"]
    write_frames(10., 25)
    rejected = batch.evaluate(attempt, job, {"gpu": 3}, 0)
    assert "gimbal_eval_track_segment_missing" in rejected["errors"]
    assert "horizon_feasible_p95_exceeded" in rejected["errors"]


def test_track_pose_preview_matches_bridge_lock_preview_and_is_off_by_default():
    from multidetect.sim_teacher.horizon_stabilization import (
        preview_body_rotation, rotation_deg, _mat_mul)
    target = flight.WorldTarget(42.001, -93, 300)
    body = {"pitch": 2., "yaw": 30., "roll": 1.}
    pos = flight.WorldTarget(42., -93, 385)
    rates = (0.0, 0.0, math.radians(60.0))
    kwargs = dict(now_s=1., attitude_time_s=1., position=pos)
    plain = BridgeHorizonTarget({"pitch": -45., "yaw": 0., "roll": 0.}, mode="track", track_targets=(target,))
    previewed = BridgeHorizonTarget({"pitch": -45., "yaw": 0., "roll": 0.}, mode="track",
                                    track_targets=(target,), track_preview_s=0.2)
    base = plain.update(body, **kwargs)
    # Off by default and a no-op without rates or with zero rates.
    assert plain.update(body, body_rates_rad_s=rates, **kwargs)["quaternion"] == base["quaternion"]
    assert previewed.update(body, **kwargs)["quaternion"] == base["quaternion"]
    assert previewed.update(body, body_rates_rad_s=(0., 0., 0.), **kwargs)["quaternion"] == base["quaternion"]
    led = previewed.update(body, body_rates_rad_s=rates, **kwargs)
    assert led["quaternion"] != base["quaternion"]
    assert led["desired_earth_deg"] == base["desired_earth_deg"]  # recorded target is unchanged
    # Bridge relative pose with the commanded (led) pose equals the bridge's own preview solve.
    desired = rotation_deg(base["desired_earth_deg"])
    body_r = rotation_deg(body)
    solve = preview_body_rotation(body_r, rates, 0.2)
    t = lambda m: (m[0], m[3], m[6], m[1], m[4], m[7], m[2], m[5], m[8])
    want = _mat_mul(t(solve), desired)
    q = led["quaternion"]
    # Rebuild the led pose from the quaternion's Euler angles via the same path used by the sender.
    from multidetect.sim_teacher import horizon_stabilization as hs
    got_angles = hs.earth_angles_deg(_mat_mul(_mat_mul(body_r, t(preview_body_rotation(hs._IDENTITY, rates, 0.2))), _mat_mul(t(body_r), desired)))
    assert q == pytest.approx(hs._attitude_quaternion(got_angles))
    got = _mat_mul(t(body_r), rotation_deg(got_angles))
    assert got == pytest.approx(want, abs=1e-9)
    with pytest.raises(ValueError):
        BridgeHorizonTarget({"pitch": -45., "yaw": 0., "roll": 0.}, mode="track", track_targets=(target,),
                            track_preview_s=2.0)


def test_x1_track_preaim_only_on_the_hover_return_step_before_the_hold():
    from types import SimpleNamespace
    from multidetect.sim_teacher.gimbal_recording import x1_track_preaim
    ret = SimpleNamespace(reason="hover_return_to_home")
    assert x1_track_preaim(7, 6, ret)
    assert not x1_track_preaim(7, 7, ret) and not x1_track_preaim(7, 5, ret)
    assert not x1_track_preaim(7, 6, SimpleNamespace(reason="back_transition"))
    assert not x1_track_preaim(None, 6, ret)
    assert not x1_track_preaim(7, 6, SimpleNamespace())


def test_x1_gimbal_capture_spans_the_hold_with_a_slow_frame_server():
    """A request/response server answering in ~0.33 s wall must not make the capture lag the hold."""
    start = 100_000_000_000
    capture = X1TrackCapture(start)
    asked = []
    reader = SimpleNamespace(latest=None, request=asked.append)
    samples, pending = [], None  # (arrival_wall_s, stamp)
    for i in range(3000):  # 60 sim seconds at 50 Hz action ticks
        now = start + i * 20_000_000
        wall = i / 50.0
        if pending is not None and wall >= pending[0]:
            reader.latest = SimpleNamespace(sensor_stamp_ns=pending[1])
            pending = None
        before = len(asked)
        sample = capture.tick(reader, sim_ns=now, wall_s=wall)
        if len(asked) > before and pending is None:
            # strictly-after semantics: first 10 Hz frame stamped > asked-for stamp, 0.33 s later
            frame = ((asked[-1] // 100_000_000) + 1) * 100_000_000
            pending = (wall + 0.33, max(frame, asked[-1] + 1))
        if sample is not None:
            samples.append(sample.sensor_stamp_ns)
    spans = (samples[-1] - samples[0]) / 1e9
    assert len(samples) >= 120 and spans >= 55, (len(samples), spans)
    gaps = [(b - a) / 1e9 for a, b in zip(samples, samples[1:])]
    assert max(gaps) < 1.0 and all(g >= 0.1 - 1e-9 for g in gaps)

def test_track_preview_cap_and_override_plumbing():
    import math
    from multidetect.sim_teacher.gimbal_recording import x1_track_preview
    from multidetect.sim_teacher.horizon_stabilization import (
        BRIDGE_MAX_PREVIEW_RAD, BridgeHorizonTarget, preview_body_rotation)
    assert x1_track_preview({}) == (0.2, math.radians(12.0))
    assert x1_track_preview({"MULTIDETECT_X1_TRACK_PREVIEW_S": "0.35",
                             "MULTIDETECT_X1_TRACK_PREVIEW_MAX_DEG": "30"}) == (0.35, math.radians(30))
    for bad in ({"MULTIDETECT_X1_TRACK_PREVIEW_S": "2"}, {"MULTIDETECT_X1_TRACK_PREVIEW_MAX_DEG": "0"},
                {"MULTIDETECT_X1_TRACK_PREVIEW_S": "nan"}):
        with pytest.raises(ValueError):
            x1_track_preview(bad)
    ident = (1., 0., 0., 0., 1., 0., 0., 0., 1.)
    rates = (0., 0., math.radians(100.))  # 100 deg/s yaw sweep
    capped = preview_body_rotation(ident, rates, 0.3)
    wider = preview_body_rotation(ident, rates, 0.3, math.radians(40))
    angle = lambda m: math.degrees(math.acos((m[0] + m[4] + m[8] - 1) / 2))
    assert angle(capped) == pytest.approx(12.0, abs=1e-6) and angle(wider) == pytest.approx(30.0, abs=1e-6)
    with pytest.raises(ValueError):
        BridgeHorizonTarget({"pitch": -45., "yaw": 0., "roll": 0.}, track_preview_max_rad=0.0)
    assert BridgeHorizonTarget({"pitch": -45., "yaw": 0., "roll": 0.}).track_preview_max_rad == BRIDGE_MAX_PREVIEW_RAD


def test_x1_yaw_unwind_hysteresis_and_time_bound():
    import math
    from multidetect.sim_teacher.gimbal_recording import X1YawUnwind
    u = X1YawUnwind()
    assert u.step(None, 0.0) is None and u.step(math.nan, 0.0) is None
    assert u.step(math.radians(100), 0.0) is None  # below the entry threshold
    assert u.step(math.radians(-200), 1.0) == "lock"  # winding accumulated: unwind
    assert u.step(math.radians(-90), 1.5) is None  # still unwinding
    assert u.step(math.radians(-10), 2.0) == "track"  # near zero: resume tracking
    assert u.step(math.radians(-10), 2.1) is None
    assert u.step(math.radians(170), 3.0) == "lock"
    assert u.step(math.radians(160), 3.0 + X1YawUnwind.MAX_S + 0.1) == "track"  # time bound
