"""V2 quiet-parameter import seam, injected CPU lifecycle only."""

import pytest
from test_run_onboard_teacher_sim_operator import harness as v1_harness

from scripts import onboard_teacher_sim_operator as v1_operator
from scripts import onboard_teacher_sim_operator_v2 as v2_operator
from scripts import run_onboard_teacher_sim_operator_v2 as driver


@pytest.fixture
def harness(tmp_path, monkeypatch):
    value = v1_harness.__wrapped__(tmp_path, monkeypatch)
    monkeypatch.setattr(v2_operator, "SimOperator", v1_operator.SimOperator)
    return value


def test_v2_plan_only_does_not_start_or_write(harness):
    result = driver.observe_sim_operator(harness.plan, harness.output, artifact_root=harness.root)
    assert result["contract"].endswith("-v2") and result["status"] == "planned"
    assert not harness.output.exists() and harness.events == []


def test_v2_injected_flow_preserves_cpu_boundary_and_owner_order(harness):
    result = driver.observe_sim_operator(
        harness.plan,
        harness.output,
        execute=True,
        operator_intent=harness.intent,
        observation_s=1,
        session_factory=harness.factory,
        capture_factory=harness.capture,
        native_runner=harness.native_prefix_runner,
        artifact_root=harness.root,
    )
    assert result["status"] == "SIM-operator-observations-recorded"
    assert result["contract"].endswith("-v2")
    assert result["injected_dependencies_used"] and not result["external_SIM_observed_here"]
    assert result["session"]["cleanup_verified"]
    assert not result["context_qualified"] and not any(result["claims"].values())
