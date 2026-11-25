from __future__ import annotations

import gzip
import json
import os
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from multidetect.autonomy.airframe_profile import load_airframe_profile
from multidetect.autonomy.mission_executor import MissionExecutor, plan_for_find_task
from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.x1_contingency_executor import (
    DECISION_SCHEMA, Admission, HoldSite, MotionProof, RuntimeFrame, X1ContingencyExecutor,
    X1AirbornePlanAdapter, X1Envelope, decode_decision,
)
from multidetect.autonomy.x1_contingency_replay import load_faults, replay_states
from multidetect.autonomy.x1_contingency_state import X1StateBuilder, compact_json
from multidetect.onboard_runtime.vehicle import CommandResult

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "configs/airframes/delta_tailsitter_gimbal.json"

def mission_and_executor(plan=None):
    raw = {
        "mission_id": "x1-demo", "task_text": "Inspect and return", "risk_level": "low", "link_loss_policy": {"action": "continue", "timeout_s": 30},
        "home": [0, 0], "places": {"poi:a": [10, 0]}, "zones": {"zone:a": {"centre_ne": [10, 0], "radius_m": 4}},
        "routes": {}, "oversight_steps": [], "heartbeat_timeout_s": 5,
        "operating_area": {"north_min_m": -30, "north_max_m": 30, "east_min_m": -30, "east_max_m": 30, "min_altitude_m": 5, "max_altitude_m": 60},
        "budget": {"time_s": 600, "reserve_time_s": 60, "energy_wh": 300, "reserve_energy_wh": 30},
    }
    pkg = load_mission_package(raw)
    mission = MissionExecutor(plan or plan_for_find_task("blue car"), loop_factory=lambda *a, **k: None,
                              refs={"poi:a": [10, 0]}, mission_package=pkg)
    return pkg, mission


def good_proof(frame_id, digest, *, time_s=150):
    return MotionProof(frame_id, digest, True, 3, 8, 18, 65, 12, 18, 30, 2, 1,
                       1, 30, time_s, 400, landing_site_clear=True, min_transition_agl_m=45)


class Vehicle:
    def __init__(self): self.calls=[]
    def rtl(self): self.calls.append(("rtl",))
    def land(self): self.calls.append(("land",))
    def set_mode(self, mode): self.calls.append(("mode", mode))
    def vtol_transition(self, to): self.calls.append(("transition", to)); return CommandResult("accepted", 3000, 0)
    def loiter_at(self, *args, **kwargs): self.calls.append(("loiter", args, kwargs)); return CommandResult("accepted", 34, 0)
    def gimbal_control(self, **kwargs): self.calls.append(("gimbal", kwargs)); return CommandResult("accepted", 1000, 0)


def controller():
    pkg, mission = mission_and_executor()
    vehicle=Vehicle(); owned=[]
    ctl=X1ContingencyExecutor(mission, vehicle, pkg, X1Envelope(load_airframe_profile(PROFILE)), claim_native=lambda: owned.append(True) or True)
    return ctl, mission, vehicle, owned


def setup_action(ctl, action, params, *, state="fw", holds=(), event_id=4):
    snapshot={"schema":"x1.contingency.state.v1", "mission_id":"x1-demo", "revision":1, "t_ms":1000,
              "latest_event_id":event_id, "telemetry":{}, "events":[{"id":event_id,"t_ms":900}]}
    request=ctl.open_request(snapshot, run_id="test", hold_refs=holds)
    frame=RuntimeFrame(7, 1100, "x1-demo", 1, event_id, False, True, state, 50, 16 if state=="fw" else None,
                       True, {h: HoldSite("fw",1,2,30,40,15) for h in holds})
    wire=json.dumps({"schema":DECISION_SCHEMA, "request_id":request["request_id"], "event_id":event_id, "mission_id":"x1-demo", "revision":1,
                     "action":action, "params":params})
    return wire, frame


def test_state_mavlink_source_age_and_no_false_gcs_link():
    b=X1StateBuilder()
    b.ingest_mavlink(1000,{"_source_system":1,"_source_component":1,"message_type":"HEARTBEAT","base_mode":128,"system_status":4})
    b.ingest_mavlink(1200,{"_source_system":1,"_source_component":1,"message_type":"GPS_RAW_INT","fix_type":3,"satellites_visible":10})
    b.ingest_mavlink(1400,{"_source_system":1,"_source_component":1,"message_type":"EXTENDED_SYS_STATE","vtol_state":4,"landed_state":2})
    b.ingest_mavlink(1500,{"_source_system":0,"_source_component":0,"message_type":"HEARTBEAT","base_mode":0,"system_status":1})
    snap=b.snapshot(2000,mission_id="test",revision=1)
    assert snap["telemetry"]["fc"]=={"age_ms":1000,"source":"mavlink","armed":True,"system_status":4}
    assert snap["telemetry"]["link"] is None
    assert snap["telemetry"]["vtol"]["state"]=="fw"
    assert len(compact_json(snap)) < 2500


def test_state_normalization_rejects_nan_and_malicious_text():
    b=X1StateBuilder()
    with pytest.raises(ValueError): b.observe(1,"wind",{"north_mps":float("nan")},source="mavlink")
    with pytest.raises(ValueError): b.observe(1,"target",{"track_ref":"zone:near\\nINJECT"},source="tracker")
    b.observe(1,"agl",{"m":4},source="range")
    assert b.snapshot(2002,mission_id="test",revision=1)["telemetry"]["agl"] is None


def test_replay_expired_events_and_recording_fixture(tmp_path):
    mav=tmp_path/"mavlink.jsonl.gz"
    with gzip.open(mav,"wt") as f:
        for ms,msg in [(0,"HEARTBEAT"),(1000,"BATTERY_STATUS")]:
            row={"receive_monotonic_ns":ms*1_000_000,"_source_system":1,"_source_component":1,"message_type":msg}
            row.update({"base_mode":128,"system_status":4} if msg=="HEARTBEAT" else {"id":0,"battery_remaining":75,"charge_state":1})
            f.write(json.dumps(row)+"\n")
    states=list(replay_states(mav,mission_id="test",every_ms=500))
    assert len(states)==3 and states[-1]["telemetry"]["battery"]["pct"]==75


def test_recorded_episode_and_all_faults():
    fixture=Path(os.environ.get("X1_CONTINGENCY_EPISODE") or "/mnt/nas/AdminHome/volatic/x1-gimbal-eval/collection/rw_city_singapore__overcast/1c58e3019dcd8449bdab/jobs/fbfe3beb22a823bae954cfb6423ef8797b23567f4818feda4528ddbbdd4666ed/attempt-1/flight/episodes/episode-0000-6352323b/mavlink.jsonl")
    if not fixture.is_file():
        pytest.skip("optional NAS episode unavailable")
    rows=list(replay_states(fixture,mission_id="x1-replay",every_ms=1000,until_ms=160_000))
    assert len(rows)==161 and sum((r["telemetry"]["vtol"] or {}).get("state")=="fw" for r in rows)>0
    faults=load_faults(ROOT/"configs/simulation/x1-contingency-faults-v1.json")
    assert {f.kind for f in faults}=={"wind_step","battery_drop","link_loss","target_lost","obstacle_conflict","terrain_conflict","gps_degraded","health_degraded","transition_problem"}
    injected=list(replay_states(fixture,mission_id="x1-replay",every_ms=1000,until_ms=40000,faults=faults))
    types={e["type"] for r in injected for e in r["events"]}
    assert types=={f.kind for f in faults}
    assert next(r for r in injected if r["t_ms"]==16000)["telemetry"]["link"]["gcs"]=="down"


def test_fault_schema_rejects_bad_clock_and_overlap(tmp_path):
    p=tmp_path/"faults.json"
    p.write_text(json.dumps({"schema":"x1.contingency.faults.v1","faults":[
        {"at_ms":0,"duration_ms":20,"type":"link_loss","values":{}},
        {"at_ms":10,"duration_ms":20,"type":"link_loss","values":{}}]}))
    with pytest.raises(ValueError,match="overlapping"): load_faults(p)


def test_decode_decision_rejects_duplicate_nonfinite_unknown_and_raw_coordinates():
    bad=['{"schema":"x1.contingency.decision.v1","schema":"x1.contingency.decision.v1"}',
         '{"x":NaN}', '{"action":"setpoint","lat":1,"lon":2}']
    for wire in bad:
        with pytest.raises(ValueError): decode_decision(wire)
    ctl,_,_,_=controller(); wire,frame=setup_action(ctl,"return",{})
    assert decode_decision(wire).action=="return"


def test_stale_ticket_missing_proof_and_reject_only():
    ctl,_,vehicle,_=controller(); wire,frame=setup_action(ctl,"return",{})
    result=ctl.apply(wire,frame,now_ms=1200)
    assert result==Admission("rejected","missing_or_unbound_motion_proof","return") and not vehicle.calls
    result=ctl.apply(wire,frame,now_ms=1200,proof=good_proof(frame.frame_id,decode_decision(wire).digest))
    assert result.reason=="unmatched_or_consumed_request" and not vehicle.calls


def test_return_claims_writer_before_direct_rtl_and_does_not_confirm_ack_as_completion():
    ctl,mission,vehicle,owned=controller(); wire,frame=setup_action(ctl,"return",{})
    d=decode_decision(wire); result=ctl.apply(wire,frame,now_ms=1200,proof=good_proof(7,d.digest))
    assert result==Admission("sent_unconfirmed","readback_required","return")
    assert owned==[True] and ctl.native_owner=="return" and vehicle.calls==[("rtl",)]
    assert mission.plan_version==1


def test_fw_loiter_uses_only_offered_center_and_physical_turn_radius():
    ctl,_,vehicle,_=controller(); wire,frame=setup_action(ctl,"loiter",{"hold_ref":"orbit"},holds=("orbit",))
    d=decode_decision(wire); result=ctl.apply(wire,frame,now_ms=1200,proof=good_proof(7,d.digest))
    assert result==Admission("accepted_unconfirmed","readback_required","loiter")
    assert vehicle.calls[0][0]=="loiter" and vehicle.calls[0][2]["speed"]==15
    ctl2,_,vehicle2,_=controller(); wire2,frame2=setup_action(ctl2,"loiter",{"hold_ref":"orbit"},holds=())
    d2=decode_decision(wire2); result2=ctl2.apply(wire2,frame2,now_ms=1200,proof=good_proof(7,d2.digest))
    assert result2.reason=="hold_ref_not_offered" and not vehicle2.calls


def test_front_abort_requires_verified_enablement_and_altitude():
    ctl,_,vehicle,_=controller(); wire,frame=setup_action(ctl,"abort-transition",{},state="front_transition")
    d=decode_decision(wire); proof=good_proof(7,d.digest)
    assert ctl.apply(wire,frame,now_ms=1200,proof=proof).reason=="transition_abort_not_admitted"
    ctl,_,vehicle,_=controller(); wire,frame=setup_action(ctl,"abort-transition",{},state="front_transition")
    frame=replace(frame, front_abort_enabled=True, agl_m=20)
    d=decode_decision(wire)
    assert ctl.apply(wire,frame,now_ms=1200,proof=good_proof(7,d.digest)).reason=="transition_agl_limit"
    assert not vehicle.calls


@pytest.mark.parametrize(("overrides", "reason"), [
    ({"max_bank_deg": 36}, "predicted_dynamics_limit"),
    ({"available_energy_wh": 10}, "energy_or_reserve_limit"),
    ({"required_energy_wh": 100, "available_energy_wh": 500}, "energy_or_reserve_limit"),
    ({"terrain_margin_m": -1}, "unverified_clearance"),
    ({"required_time_s": 560, "available_time_s": 900}, "time_or_reserve_limit"),
])
def test_bad_dynamics_energy_and_task_reserve_are_rejected(overrides, reason):
    ctl, _, vehicle, _ = controller()
    wire, frame = setup_action(ctl, "continue", {})
    proof = replace(good_proof(7, decode_decision(wire).digest), **overrides)
    assert ctl.apply(wire, frame, now_ms=1200, proof=proof).reason == reason
    assert not vehicle.calls


def test_ret_task_conflicting_transition_is_rejected_without_hidden_fallback():
    ctl,_,vehicle,_=controller(); wire,frame=setup_action(ctl,"land-now",{},state="fw")
    d=decode_decision(wire); result=ctl.apply(wire,frame,now_ms=1200,proof=good_proof(7,d.digest))
    assert result.reason=="land_now_requires_mc" and not vehicle.calls and ctl.native_owner is None


def test_contingency_replan_interrupts_active_step_only_at_serial_tick():
    ctl,mission,vehicle,_=controller(); before=mission._run
    new=plan_for_find_task("red van"); wire,frame=setup_action(ctl,"retask",{"plan":plan_document(new)})
    d=decode_decision(wire); result=ctl.apply(wire,frame,now_ms=1200,proof=good_proof(7,d.digest))
    assert result.status=="installed" and mission.plan_version==2 and mission._run is not before
    assert mission.mission_log[-1]["event"]=="onboard_replan_installed"


def test_link_loss_never_becomes_host_selected_action():
    ctl,_,vehicle,_=controller(); snapshot={"schema":"x1.contingency.state.v1","mission_id":"x1-demo","revision":1,
      "t_ms":1000,"latest_event_id":1,"telemetry":{"link":{"gcs":"down"}},"events":[{"id":1,"t_ms":1000}]}
    req=ctl.open_request(snapshot,run_id="link")
    assert req["native_owner"] is None and vehicle.calls==[]


def plan_document(plan):
    return asdict(plan) | {"steps": [asdict(step) for step in plan.steps], "report": list(plan.report)}


@pytest.mark.parametrize(("action", "params", "state", "status", "calls"), [
    ("continue", {}, "fw", "continued", []),
    ("land-now", {}, "mc", "sent_unconfirmed", [("land",)]),
    ("abort-transition", {}, "front_transition", "accepted_unconfirmed", [("transition", "mc")]),
    ("gimbal-retarget", {"pitch_deg": -20, "yaw_deg": 5}, "fw", "accepted_unconfirmed",
     [("gimbal", {"pitch_deg": -20, "yaw_deg": 5})]),
])
def test_exact_action_dispatch(action, params, state, status, calls):
    ctl, _, vehicle, _ = controller()
    wire, frame = setup_action(ctl, action, params, state=state)
    frame = replace(frame, front_abort_enabled=True, gimbal_available=True)
    proof = good_proof(7, decode_decision(wire).digest)
    assert ctl.apply(wire, frame, now_ms=1200, proof=proof).status == status
    assert vehicle.calls == calls


def test_superseded_stale_and_replayed_requests_cannot_send():
    ctl, _, vehicle, _ = controller()
    old_wire, _ = setup_action(ctl, "return", {})
    wire, frame = setup_action(ctl, "return", {}, event_id=5)
    assert ctl.apply(old_wire, frame, now_ms=1200).reason == "unmatched_or_consumed_request"
    assert ctl.apply(wire, frame, now_ms=1800).reason == "decision_or_frame_stale"
    assert ctl.apply(wire, frame, now_ms=1800).reason == "unmatched_or_consumed_request"
    with pytest.raises(ValueError, match="current event"):
        setup_action(ctl, "return", {}, event_id=5)
    assert not vehicle.calls


def test_px4_owns_and_rejects_every_model_action():
    ctl, _, vehicle, _ = controller()
    wire, frame = setup_action(ctl, "gimbal-retarget", {"pitch_deg": 0, "yaw_deg": 0})
    assert ctl.apply(wire, replace(frame, px4_failsafe=True), now_ms=1200).reason == "px4_owns_or_flight_state_unknown"
    assert not vehicle.calls


@pytest.mark.parametrize(("runner_accepts", "expected_status", "revision"), [
    (True, "installed", 2), (False, "executor_failed", 1),
])
def test_x1_airborne_suffix_reaches_primitive_runner(runner_accepts, expected_status, revision):
    pkg, _ = mission_and_executor()
    calls = []

    def install(plan, old_revision):
        calls.append((tuple(step.skill for step in plan.steps), old_revision))
        return runner_accepts

    adapter = X1AirbornePlanAdapter(1, replace_remaining=install)
    vehicle = Vehicle()
    caps = frozenset({"airframe:x1"})
    ctl = X1ContingencyExecutor(adapter, vehicle, pkg, X1Envelope(load_airframe_profile(PROFILE)),
                                claim_native=lambda: False, capabilities=caps)
    plan = {"plan_version": 1, "goal": "End observation and recover", "report": [], "steps": [
        {"id": "back", "skill": "back_transition", "params": {}, "on_success": "home", "on_fail": "home"},
        {"id": "home", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None},
    ]}
    wire, frame = setup_action(ctl, "retask", {"plan": plan})
    proof = good_proof(7, decode_decision(wire, capabilities=caps).digest)
    assert ctl.apply(wire, frame, now_ms=1200, proof=proof).status == expected_status
    assert adapter.plan_version == revision and calls == [(('back_transition', 'return_home'), 1)]
    assert not vehicle.calls


def test_event_metadata_and_fault_window_does_not_restore_unobserved_link(tmp_path):
    mavlink = tmp_path / "mavlink.jsonl"
    mavlink.write_text(json.dumps({"receive_monotonic_ns": 0}) + "\n")
    sidecar = tmp_path / "observations.jsonl"
    sidecar.write_text(json.dumps({"t_ms": 500, "source": "tracker", "event": "target_lost",
                                   "confidence": 0.75, "severity": "warning"}) + "\n")
    faults_path = tmp_path / "faults.json"
    faults_path.write_text(json.dumps({"schema": "x1.contingency.faults.v1", "faults": [
        {"at_ms": 200, "duration_ms": 900, "type": "link_loss", "values": {}},
    ]}))
    states = {state["t_ms"]: state for state in replay_states(mavlink, mission_id="test", sidecar=sidecar,
                                                            faults=load_faults(faults_path))}
    assert states[200]["telemetry"]["link"]["gcs"] == "down"
    assert states[500]["events"][-1]["confidence"] == 0.75
    assert states[500]["events"][-1]["severity"] == "warning"
    assert states[1100]["telemetry"]["link"] is None


def test_deadline_uses_event_time_and_proof_covers_transition_altitude():
    ctl, _, vehicle, _ = controller()
    wire, frame = setup_action(ctl, "return", {})
    frame = replace(frame, t_ms=3901)
    assert ctl.apply(wire, frame, now_ms=3901).reason == "decision_or_frame_stale"
    ctl, _, _, _ = controller()
    wire, frame = setup_action(ctl, "return", {})
    proof = replace(good_proof(7, decode_decision(wire).digest), min_transition_agl_m=None)
    assert ctl.apply(wire, frame, now_ms=1200, proof=proof).reason == "predicted_transition_agl_limit"
    assert vehicle.calls == []
