from __future__ import annotations

import pytest
from types import SimpleNamespace

from multidetect.autonomy.onboard_loop import Detection
from onboard_factories import make_llama_runner, make_policy
from test_onboard_health import _observation


class FaultRunner:
    def __init__(self, failure):
        self.failure = failure
        self.calls = 0

    def __call__(self, row):
        self.calls += 1
        if self.failure == "process_exit":
            raise RuntimeError("llama-server exited")
        if self.failure == "http_timeout":
            raise TimeoutError("request timed out")
        if self.failure == "connection_refused":
            raise ConnectionRefusedError("connection refused")
        if self.failure == "invalid_output":
            return "not-json", 0.01
        raise OSError("model switch unavailable")


@pytest.mark.parametrize("failure", [
    "process_exit", "http_timeout", "connection_refused", "invalid_output",
    "repeated", "model_switch",
])
@pytest.mark.parametrize("mission", ["find", "count", "monitor"])
def test_slow_faults_hold_without_identity_votes(tmp_path, failure, mission):
    runner = FaultRunner("process_exit" if failure == "repeated" else failure)
    policy = make_policy(
        work_dir=tmp_path / f"{mission}-{failure}",
        config={"mission": "find", "health": {"enabled": True, "slow_timeout_s": 0.05}},
        slow_runner=runner,
        proposer=lambda frame: [Detection((20, 20, 80, 80), 0.9, "candidate")],
    )
    policy.slow_restart_budget_s = 0.01
    policy.slow_restart_limit = 1
    policy._make_identity_runner = lambda: FaultRunner("process_exit")
    policy.slow = runner
    policy.health.runner = runner
    before_rejections = len(getattr(policy.loop, "_rejections", ()))
    action, labels = policy.act(_observation(), pose_ned=(0, 0, -20), yaw_deg=0)
    assert labels["onboard"].get("state") == "health_hold"
    assert policy.slow_calls == (1 if failure == "invalid_output" else 0)
    assert len(getattr(policy.loop, "_rejections", ())) == before_rejections
    assert runner.calls <= 2
    policy.close()


def test_onboard_defaults_bound_timeout_and_restart_budget(tmp_path):
    policy = make_policy(work_dir=tmp_path)
    assert policy.slow_call_timeout_s == 5.0
    assert policy.slow_restart_budget_s == 2.0
    assert policy.slow_restart_limit == 1
    assert policy.health.config.enabled


def test_fake_llama_factory_does_not_start_a_server():
    assert make_llama_runner().process.poll() == 0


def test_operator_link_fault_is_logged_without_low_confidence_vote(monkeypatch, tmp_path):
    from test_onboard_operator_model_split import _setup

    policy, package, *_ = _setup(monkeypatch, tmp_path)
    supervisor = policy.admit_operator_package(package)
    supervisor.plan_approved = True
    policy.slow = FaultRunner("connection_refused")
    policy.health.runner = policy.slow
    policy.slow_restart_budget_s = 0
    policy.install_operator_package(supervisor)
    policy.work_dir.joinpath("frame.png").write_bytes(b"x")
    track = SimpleNamespace(track_id="candidate", bbox=(20, 20, 80, 80))
    policy._slow_identity(
        policy.work_dir / "frame.png",
        [track],
        track,
    )
    assert supervisor._low_confidence == 0
    assert "slow_model_unavailable" in supervisor.log.path.read_text(encoding="utf-8")
    policy.close()
