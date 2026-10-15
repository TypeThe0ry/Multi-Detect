from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from multidetect.onboard_runtime.health import HealthConfig, HealthMonitor
from multidetect.sim_teacher.primitives import ActionPrimitive
from scripts import onboard_policy
from onboard_factories import make_policy
from multidetect.autonomy.onboard_loop import Detection


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Slow:
    def __init__(self, probes=()):
        self.probes = list(probes)
        self.probe_calls = []

    def probe_health(self, *, timeout_s):
        self.probe_calls.append(timeout_s)
        return self.probes.pop(0) if self.probes else False


def _monitor(clock, resources, runner=None, **cfg):
    return HealthMonitor(
        config=HealthConfig(enabled=True, **cfg),
        clock=clock,
        resources=resources,
        runner=runner,
    )


def _image_bytes():
    import io

    out = io.BytesIO()
    Image.new("RGB", (320, 240), "white").save(out, format="PNG")
    return out.getvalue()


def _observation():
    return {
        "rgb": _image_bytes(),
        "camera_info": {"fx": 320.0, "fy": 320.0, "cx": 160.0, "cy": 120.0},
    }


def test_1_disabled_by_default_and_injectable_dependencies():
    clock = Clock()
    monitor = HealthMonitor(clock=clock, resources=lambda: {"temperature_c": 999})
    assert monitor.config.enabled is False
    assert monitor.on_frame().decision == "ok"
    assert monitor.state.last_resources == {}


def test_2_slow_failure_probe_cadence_and_recovery_requires_slow_success():
    clock, runner = Clock(), Slow([True])
    monitor = _monitor(clock, lambda: {}, runner=runner)
    assert monitor.record_slow_call(1, ok=False).decision == "hold"
    assert monitor.record_slow_call(1, ok=False).decision == "hold"
    assert monitor.on_frame().reason == "slow_unavailable"
    assert len(runner.probe_calls) == 1
    clock.t = 4.9
    monitor.on_frame()
    assert len(runner.probe_calls) == 1
    clock.t = 5.0
    monitor.on_frame()
    assert runner.probe_calls == [0.5, 0.5]
    assert monitor.on_frame().reason == "slow_unavailable"
    assert monitor.record_slow_call(1, ok=True).decision == "ok"
    assert monitor.on_frame().decision == "ok"


def test_3_hold_escalates_return_home_and_land_now_wins(monkeypatch):
    clock = Clock()
    monitor = _monitor(clock, lambda: {"temperature_c": 90}, escalate_after_s=2)
    policy = make_policy(config={"health": {"enabled": True, "escalate_after_s": 2}})
    policy.health = monitor
    calls = []
    policy.executor = SimpleNamespace()
    policy.supervisor = SimpleNamespace(_terminal="land_now", _escalate=lambda action, reason: calls.append(action),
                                        next_action=lambda **kw: (ActionPrimitive.hold(1), {}))
    clock.t = 0
    action, _ = policy.act(_observation(), pose_ned=(0, 0, -20), yaw_deg=0)
    assert action.kind == "hold"
    clock.t = 3
    action, _ = policy.act(_observation(), pose_ned=(0, 0, -20), yaw_deg=0)
    assert action.kind == "hold"
    assert calls == ["return_now"]


def test_4_failed_or_unparsable_slow_call_holds_current_frame():
    class Runner:
        def __call__(self, row):
            return "not-json", 0.1

    detections = [Detection((10, 10, 80, 80), 0.5, "object")]
    policy = make_policy(slow_runner=Runner(), proposer=lambda frame: detections)
    action, info = policy.act(_observation(), pose_ned=(0, 0, -20), yaw_deg=0)
    assert action.kind == "hold"
    assert info["onboard"]["state"] == "health_hold"


def test_5_pressure_skips_alternate_frames_and_hysteresis():
    clock, values = Clock(), [{"temperature_c": 86}, {"temperature_c": 86}, {"temperature_c": 70}, {"temperature_c": 86}]
    monitor = _monitor(clock, lambda: values.pop(0))
    assert monitor.on_frame().detector_skip is True
    assert monitor.on_frame().detector_skip is False
    assert monitor.on_frame().decision == "ok"
    assert monitor.on_frame().detector_skip is True


def test_6_low_disk_stops_writes_and_escalates_image_mission():
    clock = Clock()
    monitor = _monitor(clock, lambda: {"disk_free_bytes": 1}, escalate_after_s=2)
    assert monitor.on_frame(mission_requires_images=True).stop_frame_writes is True
    clock.t = 3
    decision = monitor.on_frame(mission_requires_images=True)
    assert decision.decision == "return_home"
    assert decision.stop_frame_writes is True


def test_7_transition_logger_errors_never_break_act():
    events = []
    def bad_logger(reason, data):
        events.append(reason)
        raise ValueError("logger down")
    monitor = _monitor(Clock(), lambda: {"disk_free_bytes": 1})
    monitor.logger = bad_logger
    assert monitor.on_frame(mission_requires_images=True).decision == "hold"
    assert monitor.on_frame(mission_requires_images=True).decision == "hold"
    assert events.count("low_disk") == 1


def test_8_from_text_uses_configured_slow_timeout(monkeypatch, tmp_path):
    seen = {}
    def fake_http(url, body, *, timeout):
        seen["timeout"] = timeout
        return {"choices": [{"message": {"content": json.dumps({"plan_version": 1, "goal": "x", "report": [], "steps": []})}}]}
    monkeypatch.setattr(onboard_policy, "build_mission_from_text", lambda task, refs, post, **kw: (post("p", {}), {}))
    monkeypatch.setattr("scripts.eval_slow_system._http_json", fake_http)
    with pytest.raises(Exception):
        make_policy(work_dir=tmp_path, config={"mission": "from_text", "health": {"slow_timeout_s": 7}})
    assert seen["timeout"] == 7.0


def test_9_healthy_enabled_identity_matches_disabled(monkeypatch):
    obs = _observation()
    monkeypatch.setattr(onboard_policy.time, "perf_counter", lambda: 1.0)
    disabled = make_policy(config={"health": {"enabled": False}})
    enabled = make_policy(config={"health": {"enabled": True}})
    enabled.health.resources = lambda: {}
    a0, i0 = disabled.act(obs, pose_ned=(0, 0, -20), yaw_deg=0)
    a1, i1 = enabled.act(obs, pose_ned=(0, 0, -20), yaw_deg=0)
    assert a0.to_dict() == a1.to_dict()
    assert json.dumps(i0, sort_keys=True, separators=(",", ":")) == json.dumps(i1, sort_keys=True, separators=(",", ":"))
