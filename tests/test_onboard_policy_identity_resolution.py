from __future__ import annotations

import base64
import io
import json
from types import SimpleNamespace

from PIL import Image
import pytest

from onboard_factories import make_llama_runner, make_policy
from scripts import eval_slow_system as slow


@pytest.fixture
def runtime(monkeypatch):
    """Exercise real runner construction and request encoding, with no server."""
    runners, requests = [], []
    monkeypatch.setattr(slow.LlamaServerRunner, "_start", lambda runner: runners.append(runner))
    monkeypatch.setattr(slow.LlamaServerRunner, "close", lambda runner, **kwargs: None)

    def http(url, body=None, **kwargs):
        requests.append(body)
        content = body["messages"][0]["content"]
        answer = {
            "plan_version": 1, "goal": "return home safely", "report": ["status"],
            "steps": [{"id": "home", "skill": "return_home", "params": {},
                       "on_success": None, "on_fail": None}],
        } if isinstance(content, str) else {
            "decision": "reject_track", "args": {"track_id": "track-1"}, "reason": "test"
        }
        return {"choices": [{"message": {"content": json.dumps(answer)}}]}

    monkeypatch.setattr(slow, "_http_json", http)
    return runners, requests


@pytest.mark.parametrize("size", [None, 448, 672])
@pytest.mark.parametrize("split", [False, True])
def test_identity_resolution_reaches_the_model_images_but_not_text_planning(
    runtime, tmp_path, size, split
):
    runners, requests = runtime
    config = {"mission": "from_text", "gguf": "identity.gguf", "fast_identity": False}
    if size is not None:
        config["identity_max_side"] = size
    if split:
        config["planning_model"] = "planner.gguf"
    policy = make_policy(work_dir=tmp_path / "policy", config=config, slow_runner=None)
    expected = 448 if size is None else size
    try:
        assert policy.slow is runners[-1]
        assert policy.slow.max_side == expected
        assert policy.config["identity_max_side"] == expected
        assert type(policy.config["identity_max_side"]) is int
        assert len(runners) == (2 if split else 1)
        if split:
            assert runners[0].identity_only is False
            assert runners[0].max_side == 448
        assert requests and all(isinstance(body["messages"][0]["content"], str) for body in requests)

        path = tmp_path / "frame.png"
        Image.new("RGB", (1280, 960)).save(path)
        policy.slow({
            "image_path": str(path), "task_text": "Find the target", "memory": "none",
            "focus_track_id": "track-1",
            "candidates": [{"track_id": "track-1", "box_xyxy_normalized": [0.4, 0.4, 0.5, 0.5]}],
        })
        content = requests[-1]["messages"][0]["content"]
        sizes = []
        for entry in content:
            if entry["type"] == "image_url":
                encoded = entry["image_url"]["url"].split(",", 1)[1]
                with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
                    sizes.append(image.size)
        assert sizes == [(expected, expected * 3 // 4), (224, 224)]
    finally:
        policy.close()


def test_same_model_path_and_restart_do_not_reuse_another_resolution(runtime, tmp_path):
    policies = [make_policy(work_dir=tmp_path, config={"identity_max_side": side}, slow_runner=None)
                for side in (448, 672)]
    try:
        assert policies[0].slow is not policies[1].slow
        assert [policy.slow.max_side for policy in policies] == [448, 672]
        restart = policies[1]._make_identity_runner()
        assert restart is not policies[1].slow
        assert restart.max_side == 672
        assert policies[0].slow.max_side == 448
    finally:
        for policy in policies:
            policy.close()


@pytest.mark.parametrize("value", [True, False, 0, -1, "672", "0", "bad", 672.0, None, [], {}])
def test_bad_resolution_fails_before_starting_a_runner(runtime, tmp_path, value):
    with pytest.raises(ValueError, match="identity_max_side must be a positive integer"):
        make_policy(work_dir=tmp_path, config={"identity_max_side": value}, slow_runner=None)
    assert runtime[0] == []


@pytest.mark.parametrize("runner", [SimpleNamespace(), make_llama_runner(max_side=448)])
def test_explicit_resolution_cannot_be_ignored_by_an_injected_runner(tmp_path, runner):
    with pytest.raises(ValueError, match="identity_max_side must match"):
        make_policy(work_dir=tmp_path, config={"identity_max_side": 672}, slow_runner=runner)


def test_injected_runner_can_explicitly_match_the_requested_resolution(runtime, tmp_path):
    runner = make_llama_runner(max_side=672)
    policy = make_policy(work_dir=tmp_path, config={"identity_max_side": 672}, slow_runner=runner)
    try:
        assert policy.slow is runner
        assert policy.config["identity_max_side"] == runner.max_side == 672
        assert runtime[0] == []
    finally:
        policy.close()
