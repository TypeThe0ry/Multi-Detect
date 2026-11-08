import json
from pathlib import Path

import pytest

from scripts.run_r15_round import Round
from scripts.collect_sim_teacher_episodes import _validate_runtime


def _scenario(tmp_path: Path, *, z: float, pitch: float, model: str = "px4_multirotor_standard") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    runtime = {
        "vehicle": {
            "model": model,
            "airframe_profile_id": (
                "px4_fixed_wing_gazebo_v1" if "fixed_wing" in model else "px4_multirotor_gazebo_v1"
            ),
            "spawn_pose": {"z_up_m": z, "pitch_rad": pitch},
        }
    }
    runtime_path = tmp_path / "world.runtime.json"
    runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
    scenario = tmp_path / "world.scenario.json"
    scenario.write_text(
        json.dumps({"channels": {"runtime_input": {"path": runtime_path.name}}}),
        encoding="utf-8",
    )
    return scenario


def test_preflight_rejects_legacy_airborne_multirotor_spawn(tmp_path: Path):
    scenario = _scenario(tmp_path, z=18.0, pitch=-0.3)
    with pytest.raises(ValueError, match="ground-level.*pitch_rad"):
        Round._collection_airframe(scenario)


def test_preflight_accepts_ground_multirotor_and_resolves_fixed_wing(tmp_path: Path):
    multirotor = _scenario(tmp_path / "mc", z=0.2, pitch=0.0)
    assert Round._collection_airframe(multirotor)[:2] == ("x500_mono_cam", None)

    fixed_wing = _scenario(
        tmp_path / "fw", z=0.2, pitch=0.0, model="px4_fixed_wing_standard"
    )
    assert Round._collection_airframe(fixed_wing)[:2] == ("rc_cessna_runway", "runway")


def test_collector_rejects_airframe_runtime_category_mismatch(tmp_path: Path):
    runtime = tmp_path / "runtime.json"
    runtime.write_text(
        json.dumps({"vehicle": {"model": "px4_fixed_wing_standard"}}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="multirotor/x500 runtime model|does not match"):
        _validate_runtime(runtime, "x500_mono_cam")


def test_round_rejects_unknown_runtime_vehicle_category(tmp_path: Path):
    scenario = _scenario(tmp_path, z=0.2, pitch=0.0, model="px4_rover_standard")
    with pytest.raises(ValueError, match="unsupported runtime vehicle model category"):
        Round._collection_airframe(scenario)


def test_expert_search_consumes_generated_observation_intent(tmp_path: Path, monkeypatch):
    import scripts.collect_sim_teacher_episodes as collector

    runtime = tmp_path / "runtime.json"
    runtime.write_text(
        json.dumps(
            {
                "task_intent": {
                    "observation": {
                        "camera_altitude_m": 24.0,
                        "camera_pitch_rad": -0.31,
                        "max_detection_range_m": 145.0,
                        "observation_waypoint": {
                            "x_east_m": 12.0,
                            "y_north_m": 34.0,
                            "z_up_m": 0.0,
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    plan = collector.CollectionPlan(
        scenario="scenario.json",
        runtime=str(runtime),
        truth="truth.json",
        teacher="expert_search",
        episodes=1,
        steps_per_episode=1,
        seed=1,
        out=str(tmp_path),
    )
    assert collector._takeoff_altitude_for_plan(plan) == (24.0, "teacher_intent_camera_altitude")
    monkeypatch.setattr(collector, "_truth_target", lambda *args, **kwargs: ((1.0, 2.0, 0.0), "target"))
    monkeypatch.setattr(collector, "_takeoff_altitude_for_plan", lambda plan: (5.0, "default"))
    monkeypatch.setattr(collector, "scenario_world_path", lambda runtime: None)
    monkeypatch.setattr(collector, "load_sdf_obstacles", lambda path: ())
    policy, _ = collector._policy_for(plan, Path("truth.json"), 1)
    assert policy.altitude_m == 24.0
    assert policy.camera_pitch_rad == 0.31
    assert policy.max_detection_range_m == 145.0
    assert policy.home_ned == (34.0, 12.0, 0.0)
    assert policy.observation_frames == 8
    assert policy.observation_waypoints == ((34.0, 12.0, -24.0),)


def test_expert_search_keeps_default_detection_range_without_intent_override(tmp_path: Path, monkeypatch):
    import scripts.collect_sim_teacher_episodes as collector

    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"task_intent": {"observation": {"camera_altitude_m": 20.0}}}), encoding="utf-8")
    plan = collector.CollectionPlan(
        scenario="scenario.json", runtime=str(runtime), truth="truth.json", teacher="expert_search",
        episodes=1, steps_per_episode=1, seed=1, out=str(tmp_path),
    )
    monkeypatch.setattr(collector, "_truth_target", lambda *args, **kwargs: ((1.0, 2.0, 0.0), "target"))
    monkeypatch.setattr(collector, "_takeoff_altitude_for_plan", lambda plan: (20.0, "default"))
    monkeypatch.setattr(collector, "scenario_world_path", lambda runtime: None)
    monkeypatch.setattr(collector, "load_sdf_obstacles", lambda path: ())
    policy, _ = collector._policy_for(plan, Path("truth.json"), 1)
    assert policy.max_detection_range_m == 80.0


def test_fixed_wing_intent_uses_camera_altitude_and_observation_route(tmp_path: Path, monkeypatch):
    import scripts.collect_sim_teacher_episodes as collector

    runtime = tmp_path / "runtime.json"
    runtime.write_text(
        json.dumps({"task_intent": {"observation": {
            "camera_altitude_m": 75.0,
            "camera_pitch_rad": -0.18,
            "observation_waypoint": {"x_east_m": 0.0, "y_north_m": 0.0, "z_up_m": 0.0},
        }}, "vehicle": {"model": "px4_fixed_wing_standard"}}), encoding="utf-8"
    )
    scenario = tmp_path / "gen_r20_w0006.scenario.json"
    scenario.write_text(json.dumps({"channels": {}}), encoding="utf-8")
    plan = collector.CollectionPlan(
        scenario=str(scenario), runtime=str(runtime), truth="truth.json", teacher="expert_search",
        episodes=1, steps_per_episode=60, seed=1, out=str(tmp_path), airframe="rc_cessna_runway",
    )
    assert collector._takeoff_altitude_for_plan(plan) == (75.0, "teacher_intent_camera_altitude")
    monkeypatch.setattr(collector, "_truth_target", lambda *args, **kwargs: ((30.0, 0.0, 0.0), "target"))
    monkeypatch.setattr(collector, "scenario_world_path", lambda runtime: None)
    monkeypatch.setattr(collector, "load_sdf_obstacles", lambda path: ())
    policy, _ = collector._policy_for(plan, Path("truth.json"), 1)
    assert policy.altitude_m == 75.0
    assert policy.observation_waypoints == ((0.0, 0.0, -75.0),)
    action = policy.next_action(pose_ned=(0.0, 0.0, 0.0), yaw_deg=0.0)
    assert action.kind == "orbit"


def test_deterministic_collector_config_failure_is_not_retried(monkeypatch, tmp_path: Path):
    import types
    import scripts.run_r15_round as round_script

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return types.SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="ValueError: unknown airframe profile: missing_profile",
        )

    monkeypatch.setattr(round_script.subprocess, "run", fake_run)
    monkeypatch.setattr(round_script, "_remove_worker_containers", lambda prefix: None)
    monkeypatch.setattr(round_script, "_docker_daemon_reachable", lambda: True)
    runner = Round.__new__(Round)
    runner.a = type("Args", (), {"retries": 4})()
    cmd = ["python", "collect.py", "--container-prefix", "w", "--out", str(tmp_path)]
    result = runner._collect_one("world:0", cmd, tmp_path)
    assert result["attempts"] == 1
    assert len(calls) == 1


def test_recorded_episode_completion_requires_final_status_and_matching_hash(tmp_path: Path):
    from scripts.run_r15_round import recorded_episode_is_complete, sha256

    episode = tmp_path / "episode.json"
    episode.write_text(json.dumps({"status": None}), encoding="utf-8")
    record = {"status": "complete", "path": str(episode), "sha256": sha256(episode)}
    assert recorded_episode_is_complete(record) is False

    episode.write_text(json.dumps({"status": "complete"}), encoding="utf-8")
    assert recorded_episode_is_complete(record) is False
    record["sha256"] = sha256(episode)
    assert recorded_episode_is_complete(record) is True


def test_collect_one_does_not_merge_failed_episode_as_complete(monkeypatch, tmp_path: Path):
    import types
    import scripts.run_r15_round as round_script

    attempt = tmp_path / "target" / "attempt-0" / "episodes" / "episode-0000"
    attempt.mkdir(parents=True)
    (attempt / "episode.json").write_text(json.dumps({"status": "failed"}), encoding="utf-8")

    monkeypatch.setattr(round_script.subprocess, "run", lambda *args, **kwargs: types.SimpleNamespace(
        returncode=0, stdout="", stderr=""
    ))
    monkeypatch.setattr(round_script, "_remove_worker_containers", lambda prefix: None)
    runner = Round.__new__(Round)
    runner.a = type("Args", (), {"retries": 0})()
    result = runner._collect_one(
        "world:0",
        ["python", "collect.py", "--container-prefix", "w", "--out", str(tmp_path / "target")],
        tmp_path / "target",
    )
    assert result["status"] == "failed"


def test_round_state_merge_keeps_verified_completion_over_later_failure():
    previous = {"status": "complete", "path": "episode.json", "sha256": "abc", "attempts_total": 1}
    failed = {"status": "failed", "attempts": 3, "failure": "timeout"}
    merged = Round.merge_episode_status(previous, failed)
    assert merged["status"] == "complete"
    assert merged["sha256"] == "abc"
    assert merged["last_attempt_status"] == "failed"
    assert merged["last_attempts"] == 3


def test_round_state_merge_replaces_failure_with_new_verified_completion():
    merged = Round.merge_episode_status(
        {"status": "failed", "attempts_total": 2},
        {"status": "complete", "path": "episode.json", "sha256": "new", "attempt": 1},
    )
    assert merged["status"] == "complete"
    assert merged["sha256"] == "new"
    assert merged["attempts_total"] == 4


def test_airframe_category_filter_selects_only_matching_scenario_options(tmp_path: Path):
    from scripts.run_r15_round import filter_collection_options

    mc = tmp_path / "mc.scenario.json"
    fw = tmp_path / "fw.scenario.json"
    options = {
        mc: ("x500_mono_cam", None, "multirotor"),
        fw: ("rc_cessna_runway", "runway", "fixed_wing"),
    }
    assert list(filter_collection_options(options, {"fixed_wing"})) == [fw]
    assert filter_collection_options(options) == options
    assert filter_collection_options(options, {"vtol"}) == {}
