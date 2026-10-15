"""Exercise host wiring and accounting with pure CPU model stubs."""

from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path

import pytest
from PIL import Image

from multidetect.autonomy.mission_executor import MissionExecutor
from multidetect.autonomy.onboard_loop import Detection, Track
from scripts import eval_slow_system, onboard_policy
from scripts.score_onboard_loop import score_episode


@pytest.fixture
def tmp_path():
    with tempfile.TemporaryDirectory(prefix=".place-memory-integration-", dir=Path.cwd()) as path:
        yield Path(path)


class _SlowStub:
    last_logprobs = None

    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, row):
        return json.dumps(
            {
                "decision": "reject_track",
                "args": {"track_id": "track-1", "region": None, "duration_s": None},
                "reason": "not the requested object",
            }
        ), 0.44


def _policy(monkeypatch, tmp_path, **config):
    monkeypatch.setattr(
        onboard_policy,
        "YoloeProposer",
        lambda *args, **kwargs: lambda image: [Detection((30.0, 30.0, 50.0, 50.0))],
    )
    monkeypatch.setattr(eval_slow_system, "LlamaServerRunner", _SlowStub)
    monkeypatch.setattr(onboard_policy, "pixel_ground_point", lambda *args, **kwargs: (20.0, 0.0))
    return onboard_policy.OnboardPolicy(
        task_text="Find a red car and record its position.",
        world_path=None,
        altitude_m=20.0,
        camera_info={"fx": 100.0, "fy": 100.0, "cx": 50.0, "cy": 50.0},
        image_size=(100, 100),
        work_dir=tmp_path,
        config=config,
    )


def _seed(loop):
    loop._last_pose = (0.0, 0.0, -20.0)
    loop._tracks["seed"] = Track("seed", (30.0, 30.0, 50.0, 50.0), 0, 0, ground=(20.0, 0.0))
    for _ in range(3):
        loop.apply_verdict("seed", "reject_track")
    del loop._tracks["seed"]


def _observation():
    image = io.BytesIO()
    Image.new("RGB", (100, 100)).save(image, format="PNG")
    return {"rgb": image.getvalue(), "camera_info": {}}


@pytest.mark.parametrize("mission", [None, "count"])
def test_policy_memory_logs_reuse_and_counts_only_model_calls(monkeypatch, tmp_path, mission):
    policy = _policy(monkeypatch, tmp_path, mission=mission, memory_enabled="true")
    loop = policy.executor.active_loop if policy.executor else policy.loop
    _seed(loop)

    _, labels = policy.act(_observation(), pose_ned=(0.0, 0.0, -20.0), yaw_deg=0.0)

    assert policy.slow_calls == 0
    call = labels["onboard"]["slow_calls"][0]
    assert call["remembered"] is True
    assert call["source"] == "place_verdict_memory"
    assert call["decision"] == "reject_track"
    assert labels["onboard"]["tracks"][0]["verdict_remembered"] is True
    assert labels["onboard"]["tracks"][0]["ground_ne"] == [20.0, 0.0]
    assert len(loop._place_memory.places[0].verdicts) == 3


@pytest.mark.parametrize("enabled", [None, "false"])
def test_policy_default_and_explicit_off_make_real_call(monkeypatch, tmp_path, enabled):
    config = {} if enabled is None else {"memory_enabled": enabled}
    policy = _policy(monkeypatch, tmp_path, **config)
    _seed(policy.loop)

    _, labels = policy.act(_observation(), pose_ned=(0.0, 0.0, -20.0), yaw_deg=0.0)

    assert policy.loop.config.memory_enabled is False
    assert policy.slow_calls == 1
    assert not labels["onboard"]["slow_calls"][0].get("remembered", False)
    assert labels["onboard"]["tracks"][0]["verdict_remembered"] is False


def test_policy_passes_memory_parameters_to_each_loop(monkeypatch, tmp_path):
    policy = _policy(
        monkeypatch,
        tmp_path,
        mission="count",
        memory_enabled="on",
        memory_radius_m="5.5",
        memory_min_verdicts="4",
        memory_max_range_ratio="0.6",
    )

    for loop in (policy.loop, policy.executor.active_loop):
        assert loop.config.memory_enabled is True
        assert loop.config.memory_radius_m == 5.5
        assert loop.config.memory_min_verdicts == 4
        assert loop.config.memory_max_range_ratio == 0.6


def test_follow_reuses_tracks_but_resets_previous_tasks_place_memory(monkeypatch, tmp_path):
    policy = _policy(monkeypatch, tmp_path, memory_enabled=True)
    executor = MissionExecutor(
        {
            "plan_version": 1,
            "goal": "find then follow car",
            "steps": [
                {"id": "search", "skill": "area_search",
                 "params": {"region": "zone:all", "query": "car"},
                 "on_success": "follow", "on_fail": "home"},
                {"id": "follow", "skill": "follow",
                 "params": {"track_ref": "track:1", "query": "car"},
                 "on_success": "home", "on_fail": "home"},
                {"id": "home", "skill": "return_home", "params": {},
                 "on_success": None, "on_fail": None},
            ],
            "report": ["findings", "positions"],
        },
        loop_factory=lambda *args, **kwargs: policy.loop,
    )
    source_loop = executor.active_loop
    _seed(source_loop)
    candidate = Track("found", (30.0, 30.0, 50.0, 50.0), 0, 0, ground=(20.0, 0.0))
    source_loop._tracks[candidate.track_id] = candidate
    assert source_loop.remembered_verdict(candidate.track_id) is not None
    finding = {"track_id": candidate.track_id, "position_ne": (20.0, 0.0)}
    executor.findings.append(finding)
    executor._finding_loops[id(finding)] = source_loop

    executor._finish("succeeded")

    assert executor.current_step.skill == "follow"
    assert executor.active_loop is source_loop
    assert executor.active_loop.tracks[candidate.track_id] is candidate
    assert executor.active_loop.remembered_verdict(candidate.track_id) is None


def test_score_excludes_remembered_calls_and_their_zero_latency(tmp_path):
    (tmp_path / "episode.json").write_text(json.dumps({"status": "complete"}), encoding="utf-8")
    row = {
        "obs_before": {"pose": {"local_ned_m": [0.0, 0.0, -20.0]}},
        "labels": {
            "onboard": {
                "slow_calls": [
                    {"decision": "reject_track", "latency_s": 0.44},
                    {"decision": "reject_track", "remembered": True, "latency_s": 0.0},
                ]
            }
        },
    }
    (tmp_path / "steps.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    result = score_episode(
        tmp_path,
        {"targets": [{"target_id": "target", "true_target": True, "pose": {
            "y_north_m": 20.0, "x_east_m": 0.0, "z_up_m": 0.0,
        }}]},
    )

    assert result["slow_calls"] == 1
    assert result["remembered_verdicts"] == 1
    assert result["slow_latency_p50_s"] == 0.44
