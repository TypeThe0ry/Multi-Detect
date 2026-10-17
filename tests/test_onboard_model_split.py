from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from multidetect.autonomy.mission_plan import validate_mission_plan
from scripts import onboard_policy


def test_default_identity_model_is_r12_without_planning_fallback():
    assert onboard_policy.DEFAULTS["gguf"].name == "slow-system-v2r12multi-merged-Q5_K_M.gguf"
    assert "planning_model" not in onboard_policy.DEFAULTS


def _plan():
    return validate_mission_plan(
        {
            "plan_version": 1,
            "goal": "return home safely",
            "steps": [
                {
                    "id": "home",
                    "skill": "return_home",
                    "params": {},
                    "on_success": None,
                    "on_fail": None,
                }
            ],
            "report": ["status"],
        }
    )


def _make(monkeypatch, tmp_path: Path, *, fail_planner: bool = False, report_error: bool = False):
    events: list[tuple[str, str]] = []
    active = {"count": 0}
    builds = {"count": 0}

    class FakeRunner:
        def __init__(self, model, mmproj=None, **kwargs):
            role = "planner" if "r14" in str(model) else "identity"
            assert active["count"] == 0
            active["count"] += 1
            events.append(("start", role))
            self.port = kwargs["port"]
            self.role = role

        def close(self):
            events.append(("close", self.role))
            active["count"] -= 1

    def fake_build(task, refs, post, **kwargs):
        builds["count"] += 1
        if builds["count"] == 1 and fail_planner:
            raise ValueError("planner rejected plan")
        if builds["count"] == 1 and report_error:
            return _plan(), {"error": "planner returned an invalid plan"}
        return _plan(), {}

    monkeypatch.setattr("scripts.eval_slow_system.LlamaServerRunner", FakeRunner)
    monkeypatch.setattr(onboard_policy, "build_mission_from_text", fake_build)
    policy = onboard_policy.OnboardPolicy(
        task_text="Find the target",
        world_path=None,
        altitude_m=20,
        camera_info={"fx": 320, "fy": 320, "cx": 160, "cy": 120},
        image_size=(320, 240),
        work_dir=tmp_path,
        config={
            "mission": "from_text",
            "gguf": "identity-r12.gguf",
            "planning_model": "planner-r14.gguf",
            "mmproj": "projector.gguf",
            "health": {"enabled": True},
        },
        proposer=lambda frame: [],
    )
    return policy, events, active, builds


def test_split_closes_planner_before_identity(monkeypatch, tmp_path):
    policy, events, active, builds = _make(monkeypatch, tmp_path)
    assert events == [("start", "planner"), ("close", "planner"), ("start", "identity")]
    assert active["count"] == 1
    assert builds["count"] == 1
    assert policy._model_runtime["planning_fallback_to_identity"] is False
    assert policy._model_runtime["swap_duration_s"] is not None
    assert policy._model_runtime["swap_completed_monotonic_s"] is not None
    assert "swap_time_s" not in policy._model_runtime
    policy.close()
    assert active["count"] == 0


def test_model_hash_cache_uses_size_and_mtime(monkeypatch, tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"planner")
    cache = tmp_path / ".model-hash-cache.json"
    expected = hashlib.sha256(b"planner").hexdigest()
    assert onboard_policy._model_sha256(model, cache_path=cache) == expected
    original = onboard_policy.hashlib.sha256

    def fail_hash(*args, **kwargs):
        raise AssertionError("cache miss unexpectedly re-hashed model")

    monkeypatch.setattr(onboard_policy.hashlib, "sha256", fail_hash)
    assert onboard_policy._model_sha256(model, cache_path=cache) == expected
    monkeypatch.setattr(onboard_policy.hashlib, "sha256", original)
    model.write_bytes(b"planner-updated")
    assert (
        onboard_policy._model_sha256(model, cache_path=cache)
        == hashlib.sha256(b"planner-updated").hexdigest()
    )
    assert json.loads(cache.read_text(encoding="utf-8"))


def test_model_hash_failure_is_unknown(monkeypatch, tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x")
    monkeypatch.setattr(
        onboard_policy.Path,
        "open",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("denied")),
    )
    assert onboard_policy._model_sha256(model, cache_path=tmp_path / "cache.json") == "unknown"


def test_split_planner_failure_falls_back_to_identity(monkeypatch, tmp_path):
    policy, events, active, builds = _make(monkeypatch, tmp_path, fail_planner=True)
    assert events == [("start", "planner"), ("close", "planner"), ("start", "identity")]
    assert builds["count"] == 2
    assert policy._model_runtime["planning_fallback_to_identity"] is True
    assert policy.health.state.planning_failures == 1
    policy.close()
    assert active["count"] == 0


def test_split_planner_report_error_uses_identity_retry(monkeypatch, tmp_path):
    policy, events, active, builds = _make(monkeypatch, tmp_path, report_error=True)
    assert events == [("start", "planner"), ("close", "planner"), ("start", "identity")]
    assert builds["count"] == 2
    assert policy._model_runtime["planning_fallback_to_identity"] is True
    assert policy.health.state.planning_failures == 1
    policy.close()
    assert active["count"] == 0


def test_split_identity_failure_blocks_initialization(monkeypatch, tmp_path):
    class FakeRunner:
        def __init__(self, model, mmproj=None, **kwargs):
            self.port = kwargs["port"]
            self.role = "planner" if "r14" in str(model) else "identity"

        def close(self):
            pass

    calls = {"count": 0}

    def fail_both(*args, **kwargs):
        calls["count"] += 1
        raise ValueError("invalid plan")

    monkeypatch.setattr("scripts.eval_slow_system.LlamaServerRunner", FakeRunner)
    monkeypatch.setattr(onboard_policy, "build_mission_from_text", fail_both)
    with pytest.raises(ValueError, match="invalid plan"):
        onboard_policy.OnboardPolicy(
            task_text="Find the target",
            world_path=None,
            altitude_m=20,
            camera_info={"fx": 320, "fy": 320, "cx": 160, "cy": 120},
            image_size=(320, 240),
            work_dir=tmp_path,
            config={
                "mission": "from_text",
                "gguf": "identity-r12.gguf",
                "planning_model": "planner-r14.gguf",
                "mmproj": "projector.gguf",
                "health": {"enabled": True},
            },
            proposer=lambda frame: [],
        )
    assert calls["count"] == 2
