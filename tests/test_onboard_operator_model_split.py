from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from onboard_factories import make_policy
from multidetect.autonomy.mission_package import load_mission_package
from scripts import onboard_policy

ROOT = Path(__file__).resolve().parents[1]


def _prompt_package():
    document = json.loads(
        (ROOT / "configs/onboard/mission-package-count-cones-example.json").read_text(
            encoding="utf-8"
        )
    )
    plan = document.pop("plan")
    document["task_text"] = "Count the cones and return home"
    return load_mission_package(document), onboard_policy.validate_mission_plan(plan)


def _setup(monkeypatch, tmp_path, *, fail_planner=False, block_start=None):
    events = []
    active = {"count": 0, "maximum": 0}
    identity_plan = []

    class FakeRunner:
        def __init__(self, model, mmproj=None, **kwargs):
            role = "planner" if "r14" in str(model) else "identity"
            if block_start is not None and role == "planner":
                block_start[0].set()
                block_start[1].wait(5)
            assert active["count"] == 0
            active["count"] += 1
            active["maximum"] = max(active["maximum"], active["count"])
            self.role, self.port = role, kwargs["port"]
            events.append(("start", role, kwargs.get("cache_ram_mib")))

        def close(self):
            events.append(("close", self.role, None))
            active["count"] -= 1

    package, plan = _prompt_package()

    def fake_build(*args, **kwargs):
        runner = policy.slow
        events.append(("plan", runner.role, None))
        assert policy._model_lock._is_owned()
        if runner.role == "planner" and fail_planner:
            raise ValueError("planner failed")
        if runner.role == "identity":
            identity_plan.append(True)
        return plan, {}

    monkeypatch.setattr("scripts.eval_slow_system.LlamaServerRunner", FakeRunner)
    monkeypatch.setattr(onboard_policy, "build_mission_from_text", fake_build)
    policy = make_policy(
        work_dir=tmp_path,
        config={
            "mission": "operator_link",
            "gguf": "identity-r18.gguf",
            "planning_model": "planner-r14.gguf",
            "mmproj": "projector.gguf",
        },
        slow_runner=None,
    )
    return policy, package, events, active, identity_plan


def test_operator_prompt_swaps_models_serially_and_restores_identity(monkeypatch, tmp_path):
    policy, package, events, active, identity_plan = _setup(monkeypatch, tmp_path)
    assert events == [("start", "identity", 0)]
    policy.prepare_operator_package(package)
    assert [event[:2] for event in events] == [
        ("start", "identity"), ("close", "identity"), ("start", "planner"),
        ("plan", "planner"), ("close", "planner"), ("start", "identity"),
    ]
    assert active["maximum"] == 1 and active["count"] == 1
    assert identity_plan == []
    assert policy._model_runtime["swap_duration_s"] is not None
    policy.close()
    assert active["count"] == 0


def test_operator_prompt_planner_failure_falls_back_to_identity(monkeypatch, tmp_path):
    policy, package, events, active, identity_plan = _setup(
        monkeypatch, tmp_path, fail_planner=True
    )
    policy.prepare_operator_package(package)
    assert [event[:2] for event in events] == [
        ("start", "identity"), ("close", "identity"), ("start", "planner"),
        ("plan", "planner"), ("close", "planner"), ("start", "identity"),
        ("plan", "identity"),
    ]
    assert policy._model_runtime["planning_fallback_to_identity"] is True
    assert identity_plan == [True]
    assert active["maximum"] == 1
    policy.close()
    assert active["count"] == 0


def test_operator_prompt_cancel_during_planner_start_closes_without_orphan(
    monkeypatch, tmp_path
):
    entered, release = threading.Event(), threading.Event()
    policy, package, events, active, _ = _setup(
        monkeypatch, tmp_path, block_start=(entered, release)
    )
    failures = []

    def prepare():
        try:
            policy.prepare_operator_package(package)
        except Exception as exc:
            failures.append(exc)

    worker = threading.Thread(target=prepare)
    worker.start()
    assert entered.wait(2)
    closer = threading.Thread(target=policy.close)
    closer.start()
    release.set()
    worker.join(5)
    closer.join(5)
    assert not worker.is_alive() and not closer.is_alive()
    assert failures and "cancelled" in str(failures[0])
    assert events[-1][:2] == ("close", "planner")
    assert active["count"] == 0 and active["maximum"] == 1


def test_operator_planning_waits_for_restart_thread_before_using_identity_port(
    monkeypatch, tmp_path
):
    entered, release = threading.Event(), threading.Event()
    restart_closed = threading.Event()
    policy, package, events, active, _ = _setup(monkeypatch, tmp_path)

    class RestartRunner:
        def __init__(self):
            self.port = policy._identity_port
            self.role = "identity"

        def __call__(self, row):
            raise RuntimeError("unexpected call")

        def close(self, **kwargs):
            restart_closed.set()
            events.append(("close", "identity", None))

    def rebuild():
        entered.set()
        release.wait(2)
        policy._slow_restart_result = RestartRunner()

    policy.slow.close()
    policy.slow = None
    policy.health.runner = None
    policy._slow_restart_thread = threading.Thread(target=rebuild)
    policy._slow_restart_thread.start()
    assert entered.wait(1)
    worker = threading.Thread(target=lambda: policy.prepare_operator_package(package))
    worker.start()
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert restart_closed.is_set()
    assert any(event[:2] == ("start", "planner") for event in events)
    assert policy.slow is not None and policy.slow.role == "identity"
    policy.close()


def test_operator_planning_supports_missing_identity_runner(monkeypatch, tmp_path):
    policy, package, events, active, _ = _setup(monkeypatch, tmp_path)
    policy.slow.close()
    policy.slow = None
    policy.health.runner = None
    policy.prepare_operator_package(package)
    assert any(event[:2] == ("start", "planner") for event in events)
    assert policy.slow is not None and policy.slow.role == "identity"
    policy.close()
    assert active["count"] == 0
