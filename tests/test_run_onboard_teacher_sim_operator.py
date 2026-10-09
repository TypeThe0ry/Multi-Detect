"""Injected CPU lifecycle, not parameter/GCS/Gazebo acceptance."""

import hashlib
from types import SimpleNamespace

import pytest
from test_run_onboard_teacher_px4_diagnostics import harness as old_harness

from scripts import onboard_teacher_sim_operator as operator
from scripts import run_onboard_teacher_sim_operator as driver


@pytest.fixture
def harness(tmp_path, monkeypatch):
    h = old_harness.__wrapped__(tmp_path)
    h.plan.docker_plan.airframe_profile_id = "px4_multirotor_gazebo_v1"
    h.plan.docker_plan.vehicle_model = "px4_multirotor_standard"
    h.plan.docker_plan.model_name = "x500_mono_cam"
    h.plan.docker_plan.stack_plan_sha256 = "a" * 64
    h.plan.docker_plan.assets = SimpleNamespace(
        map=SimpleNamespace(path=h.runtime), dem=SimpleNamespace(path=h.runtime)
    )
    h.intent = operator.DisposableSimOperatorIntent(
        "CPU-operator",
        "CPU-container",
        tuple(
            operator.SourceBinding("unused", 0, hashlib.sha256(b"").hexdigest()) for _ in range(5)
        ),
        True,
    )
    original_factory = h.factory

    def factory(*args, **kwargs):
        session = original_factory(*args, **kwargs)
        session.camera_updates = lambda: []
        session.start_camera_windows = lambda *a, **k: h.events.append("camera-start")
        session.stop_camera = lambda: h.events.append("camera-stop")
        return session

    class FakeOperator:
        def __init__(self, session, intent, directory, **kwargs):
            assert session.receiver is None
            assert intent is h.intent
            self.report = {"CPU_fixture": True, "claims": {"flight_authority": False}}

        def apply(self):
            h.events.append("CPU-operator-apply")
            return self.report

        def to_mapping(self):
            return self.report

    def native(command, *, timeout_s):
        h.native_runner(command, timeout_s=timeout_s)
        return {
            "native_exit_code": 0,
            "complete_output_observed": True,
            "reader_threads_exited": True,
            "streams": {
                name: {"prefix": raw, "all_bytes_retained": True, "observed_bytes": len(raw)}
                for name, raw in (("stdout", b"CPU-original\x00\xff"), ("stderr", b""))
            },
        }

    monkeypatch.setattr(operator, "SimOperator", FakeOperator)
    h.factory, h.native_prefix_runner = factory, native
    return h


def run(h, **kwargs):
    return driver.observe_sim_operator(
        h.plan,
        h.output,
        execute=True,
        operator_intent=h.intent,
        observation_s=1,
        session_factory=h.factory,
        capture_factory=h.capture,
        native_runner=h.native_prefix_runner,
        artifact_root=h.root,
        **kwargs,
    )


def test_plan_only_has_no_parameter_camera_or_native_effect(harness):
    result = driver.observe_sim_operator(harness.plan, harness.output, artifact_root=harness.root)
    assert result["status"] == "planned" and not harness.output.exists()
    assert harness.events == [] and not result["external_SIM_observed_here"]


def test_missing_explicit_operator_intent_refuses_before_session(harness):
    with pytest.raises(ValueError, match="intent"):
        driver.observe_sim_operator(
            harness.plan, harness.output, execute=True, artifact_root=harness.root
        )
    assert harness.events == [] and not harness.output.exists()


def test_source_bound_original_outputs_and_no_fixture_inflation(harness):
    result = run(harness)
    assert result["status"] == "SIM-operator-observations-recorded"
    assert result["injected_dependencies_used"] and not result["external_SIM_observed_here"]
    assert not any(result["claims"].values())
    assert not result["context_qualified"] and result["camera_frames"] == []
    events = harness.events
    assert events.index("CPU-operator-apply") < events.index("rx-start")
    assert events.index("camera-stop") < events.index("rx-stop") < events.index("journal-seal")
    assert events.index("journal-seal") < events.index("native-read")
    assert len(result["native_reads"]) == 20
    assert not result["receiver"]["thread_alive"] and result["receiver"]["queued_packets"] == 0
    assert result["session"]["cleanup_verified"]


def test_source_drift_prevents_native_results(harness):
    harness.config.source_drift = True
    result = run(harness)
    assert result["status"] == "diagnostics-failed"
    assert "source drifted" in result["error"]
    assert result["native_reads"] == []
    assert result["session"]["cleanup_verified"]


def test_original_primary_error_retained_with_cleanup(harness):
    harness.config.capture_error = "original append failure"
    result = run(harness)
    assert result["status"] == "diagnostics-failed"
    assert "original append failure" in result["error"]
    assert result["session"]["cleanup_verified"]
    assert not result["external_SIM_observed_here"]
