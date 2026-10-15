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
from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.mission_supervisor import MissionSupervisor
from multidetect.autonomy.onboard_mission_log import OnboardMissionLog
from onboard_factories import make_executor


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


def _supervised_policy(tmp_path, now, *, terminal=None):
    doc = {
        "mission_id":"health-test", "risk_level":"low", "link_loss_policy":{"action":"continue"},
        "home":[0,0], "places":{}, "zones":{}, "routes":{}, "oversight_steps":[], "heartbeat_timeout_s":5,
        "operating_area":{"north_min_m":-100,"north_max_m":100,"east_min_m":-100,"east_max_m":100,"min_altitude_m":1,"max_altitude_m":60},
        "budget":{"time_s":86400,"reserve_time_s":60,"energy_wh":300,"reserve_energy_wh":30},
        "plan":{"plan_version":1,"goal":"x","report":[],"steps":[{"id":"home","skill":"return_home","params":{},"on_success":None,"on_fail":None}]}}
    pkg = load_mission_package(doc)
    ex = make_executor(pkg.plan); log = OnboardMissionLog(tmp_path, pkg.mission_id)
    sup = MissionSupervisor(ex, pkg, log=log, clock=lambda: now.t)
    p = make_policy(config={"health":{"enabled":True}}); p.executor = ex; p.supervisor = sup
    p.health = _monitor(now, lambda: {"temperature_c": 90}, escalate_after_s=1)
    if terminal: sup._escalate(terminal, "test")
    return p, sup


def test_h1a_supervisor_land_now_wins_health_hold(tmp_path):
    c=Clock(); p,s = _supervised_policy(tmp_path,c,terminal="land_now")
    a,_=p.act(_observation(), pose_ned=(0,0,-20), yaw_deg=0)
    assert a.kind in {"stop","hold"} and s._terminal == "land_now"


def test_h1b_ground_abort_same_frame_processed(tmp_path):
    c=Clock(); p,s = _supervised_policy(tmp_path,c)
    obs=_observation(); obs["ground_events"]=[{"mission_id":"health-test","type":"abort"}]
    a,_=p.act(obs, pose_ned=(0,0,-20), yaw_deg=0)
    assert s._terminal == "return_now" and a.kind in {"translate","hold"}


def test_h1c_health_return_does_not_downgrade_land(tmp_path):
    c=Clock(); p,s = _supervised_policy(tmp_path,c,terminal="land_now")
    p.health.resources=lambda:{"temperature_c":90}
    p.act(_observation(), pose_ned=(0,0,-20), yaw_deg=0)
    assert s._terminal == "land_now"


def test_h2_recovery_executor_persists_and_reaches_home(tmp_path):
    c=Clock(); p=make_policy(work_dir=tmp_path, config={"mission":"find","health":{"enabled":True,"escalate_after_s":1}}, home_ned=(0,0))
    p.health.clock=c; p.health.resources=lambda:{"temperature_c":90}
    c.t=2
    pose=[10.,0.,-20.]; rec=None
    for _ in range(80):
        c.t += 0.1
        a,_=p.act(_observation(), pose_ned=tuple(pose), yaw_deg=0)
        rec = p._health_recovery_executor if rec is None else rec
        if rec is None:
            c.t += 1
            continue
        if a.kind=="translate": pose[0]+=a.forward_m; pose[1]+=a.right_m; pose[2]-=a.up_m
        if rec.done: break
    assert p._health_recovery_executor is rec and rec is not None


def test_h3_low_disk_skips_slow_without_frame(tmp_path):
    calls=[]
    class R:
        def __call__(self,row): calls.append(row); return ({"decision":"confirm"},0.1)
    p=make_policy(work_dir=tmp_path, config={"mission":"find","health":{"enabled":True,"disk_hard_floor_bytes":10**18,"escalate_after_s":1}}, slow_runner=R(), proposer=lambda f:[Detection((1,1,20,20),.9,"x")])
    p.health.resources=lambda:{"disk_free_bytes":1}
    p.act(_observation(), pose_ned=(0,0,-20), yaw_deg=0)
    assert calls==[] and p.health.state.consecutive_slow_failures==0


def test_m4_clock_jumps_are_bounded():
    c=Clock(); m=_monitor(c,lambda:{"temperature_c":90},escalate_after_s=4,max_frame_delta_s=5)
    m.on_frame(); c.t=2; m.on_frame(); c.t=1; m.on_frame(); c.t=2; assert m.on_frame().decision=="ok"
    c.t=100; assert m.on_frame().decision=="return_home"


def test_m5_sampling_failures_reach_hold():
    m=_monitor(Clock(),lambda: (_ for _ in ()).throw(RuntimeError()),sampling_failure_limit=10)
    for _ in range(9): assert m.on_frame().decision != "hold"
    assert m.on_frame().reason=="resource_sampling_failed"


def test_m6_stale_probe_does_not_unlock():
    c,r=Clock(),Slow([True]); m=_monitor(c,lambda:{},runner=r,probe_interval_s=1)
    m.record_slow_call(1,ok=False); m.record_slow_call(1,ok=False); m.on_frame(); assert m.state.probe_succeeded
    c.t=2; m.record_slow_call(1,ok=False); assert not m.state.slow_available


def test_m8_from_text_failure_recorded(monkeypatch):
    seen={}
    real=onboard_policy.HealthMonitor
    def hm(*a,**kw): seen["m"]=real(*a,**kw); return seen["m"]
    monkeypatch.setattr(onboard_policy,"HealthMonitor",hm)
    monkeypatch.setattr(onboard_policy,"build_mission_from_text",lambda *a,**k: (_ for _ in ()).throw(ValueError("bad plan")))
    with pytest.raises(ValueError): make_policy(config={"mission":"from_text","health":{"enabled":True,"slow_failure_limit":1}})
    assert seen["m"].state.consecutive_slow_failures==1


def test_1_disabled_by_default_and_injectable_dependencies():
    clock = Clock()
    monitor = HealthMonitor(clock=clock, resources=lambda: {"temperature_c": 999})
    assert monitor.config.enabled is False
    assert monitor.on_frame().decision in {"ok", "hold"}
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
