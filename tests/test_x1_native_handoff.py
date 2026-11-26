"""CPU-only live native handoff: single route writer, exact PX4 command, ACK and mode readback."""
import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_x1_mission import _build, _package, _plan, profile, terrain  # noqa: F401
from test_x1_motion_proof import _frame, _native_fixture

from multidetect.autonomy.x1_contingency_executor import (
    DECISION_SCHEMA,
    HoldSite,
    RuntimeFrame,
    X1AirbornePlanAdapter,
    X1ContingencyExecutor,
    X1Envelope,
)
from multidetect.autonomy.x1_contingency_state import X1StateBuilder
from multidetect.autonomy.x1_motion_proof import X1MotionProofBuilder
from multidetect.sim_teacher import x1_scenario
from multidetect.sim_teacher.streams import MavlinkState
from multidetect.sim_teacher.x1_native_handoff import (
    AUTO_LAND,
    AUTO_LOITER,
    AUTO_RTL,
    MAV_CMD_DO_SET_MODE,
    NativeHandoffPort,
    RouteWriteGate,
    RouteWriteRevoked,
    reader_ack_waiter,
)
from multidetect.sim_teacher.x1_scenario import CollectorScenario, SitlScenarioTransport
from multidetect.sim_teacher.x1_scenario_decision import DecisionTick, recorded_decisions

MS = 1_000_000
OFFBOARD = (6, 0)


def _custom(mode):
    main, sub = mode
    return (main << 16) | (sub << 24)


class FakeLink:
    """A pymavlink-like connection; ``mav`` records every COMMAND_LONG on the wire."""

    def __init__(self):
        self.sent = []
        self.target_system, self.target_component = 1, 1
        link = self

        class Mav:
            def command_long_send(self, *args):
                link.sent.append(("command_long", *args))

            def set_position_target_local_ned_send(self, *args):
                link.sent.append(("setpoint", *args))

        self.mav = Mav()

    def set_mode(self, mode):
        self.sent.append(("set_mode", mode))

    def wait_command_ack(self, command, timeout_s, after_ns=None):
        return {"command": command, "result": 0}


class FakeReader:
    def __init__(self, clock):
        self.clock = clock
        self.state = MavlinkState()

    def put(self, name, peer=(1, 1), **row):
        self.state.messages[name] = {"_source_system": peer[0], "_source_component": peer[1], **row}
        self.state.received_monotonic_ns[name] = self.clock[0]

    def latest_control_state(self):
        return MavlinkState(messages=dict(self.state.messages),
                            received_monotonic_ns=dict(self.state.received_monotonic_ns))


def _port(*, ack=0, mode_before=OFFBOARD, confirm_timeout_s=5.0):
    clock = [1_000 * MS]
    reader = FakeReader(clock)
    reader.put("HEARTBEAT", custom_mode=_custom(mode_before), system_status=4, base_mode=128)
    reader.put("GLOBAL_POSITION_INT", lat=520_000_000, lon=50_000_000, alt=180_000, relative_alt=80_000)
    link, gate, rows = FakeLink(), RouteWriteGate(), []
    acks = []

    def wait_ack(command, timeout_s, after_ns):
        acks.append((command, timeout_s, after_ns))
        clock[0] += 50 * MS
        return None if ack is None else {"command": command, "result": ack}

    port = NativeHandoffPort(link, reader, gate, rows.append, wait_ack=wait_ack, peer=(1, 1),
                             confirm_timeout_s=confirm_timeout_s, monotonic_ns=lambda: clock[0])
    return SimpleNamespace(port=port, link=link, gate=gate, rows=rows, reader=reader, clock=clock, acks=acks)


def _advance(fx, ms, **heartbeat):
    fx.clock[0] += ms * MS
    if heartbeat:
        fx.reader.put("HEARTBEAT", **heartbeat)


def test_gate_refuses_route_writes_after_claim_and_waits_for_inflight_send():
    link, gate = FakeLink(), RouteWriteGate()
    route = gate.wrap(link)
    route.mav.command_long_send(1, 1, 192, 0, *(0.0,) * 7)
    assert route.wait_command_ack(192, 1.0)["result"] == 0  # Reads stay available.
    release, entered = threading.Event(), threading.Event()
    original = link.mav.command_long_send

    def slow(*args):
        entered.set()
        release.wait(2)
        original(*args)

    link.mav.command_long_send = slow
    writer = threading.Thread(target=route.mav.command_long_send, args=(1, 1, 192, 0, *(1.0,) * 7))
    writer.start()
    entered.wait(2)
    claimed = []
    claimer = threading.Thread(target=lambda: claimed.append(gate.claim("native")))
    claimer.start()
    time.sleep(0.05)
    assert not claimed  # The claim waits out the route send that is already on the wire.
    release.set()
    writer.join(2)
    claimer.join(2)
    assert claimed == [True] and gate.owner == "native"
    stops = []
    gate.add_stop(lambda: stops.append("streamer"))
    for write in (lambda: route.mav.command_long_send(1, 1, 192, 0, *(2.0,) * 7),
                  lambda: route.mav.set_position_target_local_ned_send(0), lambda: route.set_mode("RTL")):
        with pytest.raises(RouteWriteRevoked):
            write()
    gate.claim("native")
    assert stops == ["streamer"] and gate.owner == "native"
    assert gate.blocked == ["mav.command_long_send", "mav.set_position_target_local_ned_send", "set_mode"]
    assert [row[3] for row in link.sent] == [192, 192]  # Nothing after the claim reached the vehicle.


@pytest.mark.parametrize(("call", "command", "params", "expected"), [
    (lambda p: p.rtl(), 20, (0.0,) * 7, AUTO_RTL),
    (lambda p: p.land(), MAV_CMD_DO_SET_MODE, (1.0, 4.0, 6.0, 0.0, 0.0, 0.0, 0.0), AUTO_LAND),
    (lambda p: p.set_mode("HOLD"), MAV_CMD_DO_SET_MODE, (1.0, 4.0, 3.0, 0.0, 0.0, 0.0, 0.0), AUTO_LOITER),
])
def test_accepted_command_is_sent_once_and_confirmed_by_mode(call, command, params, expected):
    fx = _port()
    with pytest.raises(RuntimeError, match="route writer claim"):
        call(fx.port)
    assert fx.link.sent == []
    assert fx.port.claim() is True
    result = call(fx.port)
    assert (result.ok, result.ack_result) == (True, 0)
    assert fx.link.sent == [("command_long", 1, 1, command, 0, *params)]
    assert fx.port.poll() == "pending"  # No HEARTBEAT after the ACK yet.
    if expected == AUTO_LOITER:
        _advance(fx, 100, custom_mode=_custom(expected))
        assert fx.port.poll() == "pending"  # Mode alone is not the hold point.
        fx.reader.put("POSITION_TARGET_GLOBAL_INT", lat_int=520_000_050, lon_int=50_000_000)
    else:
        _advance(fx, 100, custom_mode=_custom(expected))
    assert fx.port.poll() == "confirmed"
    assert fx.rows[-1]["message_type"] == "scenario_native_handoff"
    assert fx.rows[-1]["status"] == "confirmed" and fx.rows[-1]["observed_mode"] == list(expected)
    with pytest.raises(ValueError):
        fx.port.set_mode("RTL")


@pytest.mark.parametrize(("ack", "reason", "ok"), [(4, "ack_denied", False), (None, "ack_timeout", False)])
def test_nack_or_ack_timeout_is_unconfirmed_without_retry(ack, reason, ok):
    fx = _port(ack=ack)
    fx.port.claim()
    result = fx.port.rtl()
    assert (result.ok, result.ack_result) == (ok, ack)
    assert fx.port.status == "unconfirmed" and fx.rows[-1]["reason"] == reason
    assert len(fx.link.sent) == 1 and len(fx.acks) == 1
    _advance(fx, 100, custom_mode=_custom(AUTO_RTL))
    assert fx.port.poll() == "unconfirmed"  # Never upgraded later and never resent.
    assert len(fx.link.sent) == 1


def test_mode_not_observed_before_deadline_fails_closed():
    fx = _port(confirm_timeout_s=2.0)
    fx.port.claim()
    fx.port.rtl()
    _advance(fx, 1000, custom_mode=_custom(OFFBOARD))
    assert fx.port.poll() == "pending"
    with pytest.raises(RuntimeError, match="still unconfirmed"):
        fx.port.land()  # No overlapping native command while one is unresolved.
    _advance(fx, 1500, custom_mode=_custom(OFFBOARD))
    assert fx.port.poll() == "unconfirmed"
    assert fx.rows[-1]["reason"] == "mode_not_observed"
    assert len(fx.link.sent) == 1


def test_stale_or_foreign_heartbeat_does_not_confirm():
    fx = _port(confirm_timeout_s=1.0)
    fx.port.claim()
    fx.reader.put("HEARTBEAT", peer=(2, 1), custom_mode=_custom(AUTO_RTL))
    fx.port.rtl()
    assert fx.port.poll() == "pending"  # Other peer's HEARTBEAT, and none after the ACK.
    fx.clock[0] += 2000 * MS
    assert fx.port.poll() == "unconfirmed"


def test_mc_hold_in_unchanged_mode_needs_the_hold_point():
    fx = _port(mode_before=AUTO_LOITER, confirm_timeout_s=1.0)
    fx.port.claim()
    fx.port.set_mode("HOLD")
    fx.reader.put("POSITION_TARGET_GLOBAL_INT", lat_int=520_100_000, lon_int=50_000_000)  # Route target, 1.1 km.
    _advance(fx, 1500, custom_mode=_custom(AUTO_LOITER))
    assert fx.port.poll() == "unconfirmed" and fx.rows[-1]["reason"] == "hold_target_not_observed"


def test_fw_orbit_uses_admitted_site_verbatim():
    fx = _port()
    fx.port.claim()
    result = fx.port.loiter_at((52.0, 5.0), 80.0, 402.404, speed=15.0)
    assert result.ok
    assert fx.link.sent == [("command_long", 1, 1, 34, 0, 80.0, 15.0, 0.0, 0.0, 52.0, 5.0, 402.404)]
    _advance(fx, 100, custom_mode=_custom(AUTO_LOITER))
    fx.reader.put("POSITION_TARGET_GLOBAL_INT", lat_int=520_000_000, lon_int=50_000_000)
    assert fx.port.poll() == "confirmed"


def test_reader_ack_waiter_uses_per_command_cache():
    reader = SimpleNamespace(state=MavlinkState())
    clock = [0.0]
    wait = reader_ack_waiter(reader, sleep=lambda _: clock.__setitem__(0, clock[0] + 0.5), monotonic=lambda: clock[0])
    reader.state.command_acks[20] = ({"command": 20, "result": 0}, 100)
    assert wait(20, 1.0, after_ns=50) == {"command": 20, "result": 0}
    assert wait(20, 1.0, after_ns=200) is None  # An older ACK never answers a newer command.


def _live_tick(package, profile, port, decision, frame, *, proof, markers):  # noqa: F811
    now = [1000]
    tick = DecisionTick(package, profile, recorded_decisions((decision,)), None, markers.append,
                        run_id="native-live", plan_digest="f" * 64, now_ms=lambda: now[0],
                        build_proof=proof, native_port=port)
    try:
        tick.tick(replace(frame, t_ms=now[0]), events=(("target_lost", "tracker"),))
        tick.future.result(timeout=2)
        now[0] += 10
        tick.tick(frame)
    finally:
        tick.close()
    return tick, markers[-1]


@pytest.fixture
def admitted(terrain, profile):  # noqa: F811
    package = _package(plan=_plan(), capabilities=["gimbal_control"])
    steps = _build(package, terrain, profile)
    builder = X1MotionProofBuilder(package, profile, terrain, steps, mission_radius_m=1250,
                                   terrain_error_m=1, obstacle_clearance=lambda box: 25)
    return package, builder


@pytest.mark.parametrize(("kind", "action", "params", "command"), [
    ("return", "return", {}, 20), ("land", "land-now", {}, MAV_CMD_DO_SET_MODE),
    ("hold", "loiter", {"hold_ref": "here"}, MAV_CMD_DO_SET_MODE), ("orbit", "loiter", {"hold_ref": "orbit"}, 34),
])
def test_live_tick_admits_and_dispatches_through_the_native_port(admitted, profile, kind, action, params, command):  # noqa: F811
    package, builder = admitted
    frame, evidence = _native_fixture(kind, package)
    fx = _port()
    route = fx.gate.wrap(fx.link)
    tick, receipt = _live_tick(package, profile, fx.port, {"action": action, "params": params}, frame,
                               proof=lambda d, f: builder.build(d, f, evidence(d)), markers=fx.rows)
    assert (receipt["action"], receipt["status"], receipt["reason"]) == (action, "accepted_unconfirmed",
                                                                         "readback_required")
    assert tick.executor.native_owner == action and fx.gate.owner == "native"
    assert [row[3] for row in fx.link.sent] == [command]
    if kind == "orbit":
        hold = frame.hold_sites["orbit"]
        assert fx.link.sent[0][5:] == (hold.radius_m, hold.tangential_speed_mps, 0.0, 0.0,
                                       hold.lat_deg, hold.lon_deg, hold.alt_amsl_m)
    with pytest.raises(RouteWriteRevoked):
        route.mav.command_long_send(1, 1, 192, 0, *(0.0,) * 7)
    assert len(fx.link.sent) == 1


@pytest.mark.parametrize(("ack", "status"), [(4, "transport_denied"), (None, "transport_uncertain")])
def test_live_tick_reports_nack_and_timeout_and_keeps_route_revoked(admitted, profile, ack, status):  # noqa: F811
    package, builder = admitted
    frame, evidence = _native_fixture("return", package)
    fx = _port(ack=ack)
    tick, receipt = _live_tick(package, profile, fx.port, {"action": "return", "params": {}}, frame,
                               proof=lambda d, f: builder.build(d, f, evidence(d)), markers=fx.rows)
    assert (receipt["status"], receipt["reason"]) == (status, "no_positive_acceptance")
    # Ownership is not released on a NACK or timeout; PX4 failsafe is the floor.
    assert tick.executor.native_owner == "return" and fx.gate.owner == "native"
    assert fx.port.status == "unconfirmed" and len(fx.link.sent) == 1


@pytest.mark.parametrize("defect", ["energy_low", "missing_sweep"])
def test_live_rejection_sends_nothing_and_keeps_the_route_writer(admitted, profile, defect):  # noqa: F811
    package, builder = admitted
    frame, evidence = _native_fixture("return", package)
    if defect == "energy_low":
        frame = replace(frame, remaining_energy_wh=1)
    fx = _port()
    route = fx.gate.wrap(fx.link)
    tick, receipt = _live_tick(package, profile, fx.port, {"action": "return", "params": {}}, frame,
                               proof=lambda d, f: builder.build(d, f, None if defect == "missing_sweep" else evidence(d)),
                               markers=fx.rows)
    assert receipt["status"] == "rejected"
    assert receipt["reason"] == {"energy_low": "energy_or_reserve_limit", "missing_sweep": "unverified_clearance"}[defect]
    assert fx.gate.owner is None and tick.executor.native_owner is None and fx.link.sent == []
    route.mav.command_long_send(1, 1, 192, 0, *(0.0,) * 7)  # The compiled route keeps flying.
    assert len(fx.link.sent) == 1


def test_executor_applies_the_decision_verbatim_without_substitution(admitted, profile):  # noqa: F811
    package, builder = admitted
    frame, evidence = _native_fixture("orbit", package)
    sent = []
    vehicle = SimpleNamespace(loiter_at=lambda *a, **k: sent.append((a, k)) or SimpleNamespace(ok=True, ack_result=0),
                              rtl=lambda: sent.append("rtl"), land=lambda: sent.append("land"),
                              set_mode=lambda mode: sent.append(mode))
    executor = X1ContingencyExecutor(X1AirbornePlanAdapter(1, replace_remaining=lambda *_: False), vehicle, package,
                                     X1Envelope(profile), claim_native=lambda: True,
                                     capabilities=frozenset({"airframe:x1"}))
    state = X1StateBuilder()
    state.event(1000, "target_lost", source="tracker")
    request = executor.open_request(state.snapshot(1000, mission_id=package.mission_id, revision=1),
                                    run_id="verbatim", hold_refs=("orbit",))
    wire = json.dumps({"schema": DECISION_SCHEMA, "request_id": request["request_id"], "event_id": 1,
                       "mission_id": package.mission_id, "revision": 1, "action": "loiter",
                       "params": {"hold_ref": "orbit"}})
    # The frame offers a different (unoffered) hold: the executor must reject, not pick it.
    other = replace(frame, t_ms=1010, hold_sites={"other": HoldSite("fw", 52.0, 5.0, 180, 60, 15)})
    admission = executor.apply(wire, other, now_ms=1010, build_proof=lambda d, f: builder.build(d, f, evidence(d)))
    assert (admission.status, admission.reason) == ("rejected", "unknown_hold_site_for_mode") and sent == []


def test_battery_recovery_is_not_applicable_only_under_confirmed_native_return(monkeypatch):
    params = {"SIM_BAT_DRAIN": 1800.0, "SIM_BAT_MIN_PCT": 50.0}

    def command(*args):
        if args[1] == "set":
            params[args[2]] = float(args[3])
            return ""
        return f"{params[args[3]]}\n"

    clock = [0.0]
    monkeypatch.setattr(x1_scenario.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(x1_scenario.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    fault = SimpleNamespace(kind="battery_drop", values={"pct": 17})
    for exempt, outcome in ((None, "raise"), ("vehicle_under_native_return", "not_applicable")):
        transport = SitlScenarioTransport(command, battery_sample=lambda: 17, recovery_exempt=lambda e=exempt: e)
        transport.battery_params = 1800.0, 50.0
        transport.battery_prior = 97
        if outcome == "raise":
            with pytest.raises(RuntimeError, match="recovery was not observed"):
                transport.restore(fault)
        else:
            details = transport.restore(fault)
            assert details["recovery"] == {"status": "not_applicable", "expected_min_pct": 96,
                                           "reason": "vehicle_under_native_return"}
        assert params == {"SIM_BAT_DRAIN": 1800.0, "SIM_BAT_MIN_PCT": 50.0}  # Restored either way.
    transport = SitlScenarioTransport(command, battery_sample=lambda: 97, recovery_exempt=lambda: "unused")
    transport.battery_params, transport.battery_prior = (1800.0, 50.0), 97
    assert transport.restore(fault)["recovery"]["status"] == "observed"


def _scenario_with(port, *, failure=None):
    scenario = CollectorScenario((), clearance=lambda *_: None)
    scenario.route_gate = port.gate
    scenario.native = port
    scenario.native_failure = failure
    scenario.tick = lambda index, gimbal: port.poll()
    return scenario


def test_finish_native_waits_for_px4_landing_and_fails_closed():
    fx = _port()
    scenario = _scenario_with(fx.port)
    with pytest.raises(RuntimeError, match="requires a claimed route writer"):
        scenario.finish_native(3)
    fx.port.claim()
    with pytest.raises(RuntimeError, match="dispatched no command"):
        scenario.finish_native(3, sleep=lambda _: None)
    fx.port.rtl()
    _advance(fx, 100, custom_mode=_custom(AUTO_RTL))
    steps = iter(range(100))

    def sleep(_):
        if next(steps) == 3:
            fx.reader.put("EXTENDED_SYS_STATE", landed_state=1, vtol_state=3)

    result = scenario.finish_native(3, sleep=sleep)
    assert result["status"] == "landed" and result["method"] == "native_return" and result["vtol_state"] == 3
    assert scenario._recovery_exempt() == "vehicle_under_native_return"

    denied = _port(ack=4)
    denied.port.claim()
    denied.port.rtl()
    with pytest.raises(RuntimeError, match="unconfirmed: return; PX4 failsafe remains the floor"):
        _scenario_with(denied.port).finish_native(3, sleep=lambda _: None)
    assert _scenario_with(denied.port)._recovery_exempt() is None

    held = _port()
    held.port.claim()
    held.port.loiter_at((52.0, 5.0), 80.0, 402.4, speed=15.0)
    with pytest.raises(RuntimeError, match="hold_residence_expired"):
        _scenario_with(held.port, failure="hold_residence_expired").finish_native(1, sleep=lambda _: None)


def test_hold_revalidation_event_then_expiry():
    rows = []
    scenario = CollectorScenario((), clearance=lambda *_: None)
    scenario.record = rows.append
    scenario.hold = {"hold_ref": "hold:yard-orbit", "deadline_ms": 50_000, "revalidation_requested": False}
    events = []
    assert scenario._hold_events(39_000, events) is True and events == []
    assert scenario._hold_events(40_000, events) is True and events == [("hold_revalidation", "executor")]
    assert scenario._hold_events(41_000, events) is True and len(events) == 1
    assert scenario._hold_events(50_000, events) is False
    assert scenario.native_failure == "hold_residence_expired"
    assert [row["message_type"] for row in rows] == ["scenario_native_hold_revalidation",
                                                     "scenario_native_hold_expired"]


def test_join_stops_waiting_once_route_writes_are_revoked():
    from scripts.collect_sim_teacher_episodes import _join_action_with_gimbal

    release = threading.Event()
    worker = threading.Thread(target=lambda: release.wait(5), daemon=True)
    worker.start()
    ticks = []
    owner = []
    _join_action_with_gimbal(worker, None, lambda: ticks.append(1) or owner.append("native"),
                             abandon=lambda: bool(owner))
    assert worker.is_alive() and ticks == [1]
    release.set()
    worker.join(2)


def test_live_tick_without_native_port_still_rejects(admitted, profile):  # noqa: F811
    package, builder = admitted
    frame, evidence = _native_fixture("return", package)
    markers = []
    now = [1000]
    tick = DecisionTick(package, profile, recorded_decisions(({"action": "return", "params": {}},)), None,
                        markers.append, run_id="no-port", plan_digest="f" * 64, now_ms=lambda: now[0],
                        build_proof=lambda d, f: builder.build(d, f, evidence(d)))
    try:
        tick.tick(replace(frame, t_ms=now[0]), events=(("target_lost", "tracker"),))
        tick.future.result(timeout=2)
        now[0] += 10
        tick.tick(frame)
    finally:
        tick.close()
    assert (markers[-1]["status"], markers[-1]["reason"]) == ("rejected", "native_handoff_not_available")
    with pytest.raises(ValueError, match="CPU replay cannot take a live port"):
        DecisionTick(package, profile, None, None, markers.append, run_id="x", plan_digest="f" * 64,
                     cpu_replay=True, native_port=object())


def test_shared_native_fixture_contract(admitted):
    package, _ = admitted
    assert isinstance(_frame(package), RuntimeFrame)


def _native_bounds(*, fw):
    dynamics = dict(max_bank_deg=20, max_climb_mps=2, max_descent_mps=2, max_acceleration_mps2=1,
                    max_angular_rate_dps=30)
    leg = lambda phase, end, seconds: dict(phase=phase, end=end, padding_ne_m=2, padding_alt_m=1,  # noqa: E731
                                           duration_s=seconds, peak_power_w=500)
    mc_entry = [199, 211, -2, 2, 178, 182]
    return {
        "return": {"3": dict(entry=mc_entry, max_entry_speed_mps=12, legs=[leg("mc", [0, 0, 180], 60),
                                                                           leg("landing", [0, 0, 100], 50)],
                             dynamics=dynamics, airspeed=None, failure_revalidation_enforced=False)},
        "land-now": {"3": dict(max_entry_speed_mps=2, padding_ne_m=2, padding_alt_m=1, duration_s=50,
                               peak_power_w=500, dynamics=dynamics)},
        "holds": {
            "hold:mc": dict(mode="mc", steps=[3], entry=mc_entry, max_entry_speed_mps=6, padding_ne_m=10,
                            padding_alt_m=2, duration_s=60, peak_power_w=500, dynamics=dynamics),
            "hold:orbit": dict(mode="fw", steps=[1], entry=mc_entry, max_entry_speed_mps=30, centre=[300, 0, 180],
                               radius_m=60, tangential_speed_mps=15, padding_ne_m=70, padding_alt_m=2,
                               duration_s=90, peak_power_w=500, dynamics=dynamics, airspeed=[12, 18]),
        },
    }


def _exercise_native(terrain, profile, monkeypatch, decision, *, fw=False, mutate=None):  # noqa: F811
    import test_x1_live_clearance as live

    from multidetect.autonomy.mission_package import load_mission_package

    original = live.recorded_decisions
    monkeypatch.setattr(live, "recorded_decisions", lambda _: original((decision,)))

    def package_override(_package, doc):
        evidence = doc["x1_live_evidence"]
        evidence["obstacle_coverage"] = [-200, 600, -200, 200, 95, 260]
        evidence["native"] = _native_bounds(fw=fw)
        if mutate is not None:
            mutate(evidence)
        return load_mission_package(doc, mode="sim")

    return live.exercise(terrain, profile, monkeypatch, fw=fw, package_override=package_override)


@pytest.mark.parametrize(("decision", "fw", "command"), [
    ({"action": "return", "params": {}}, False, ("rtl",)),
    ({"action": "loiter", "params": {"hold_ref": "hold:mc"}}, False, ("set_mode", "HOLD")),
    ({"action": "loiter", "params": {"hold_ref": "hold:orbit"}}, True, "loiter_at"),
])
def test_live_evidence_proves_native_actions(terrain, profile, monkeypatch, decision, fw, command):  # noqa: F811
    # The shared fixture streams 5 m/s in MC (hold entry limit 6) and 20 m/s in FW.
    receipt, [(_, frame, proof)], commands = _exercise_native(terrain, profile, monkeypatch, decision, fw=fw)
    assert (receipt["action"], receipt["reason"]) == (decision["action"], "readback_required"), receipt
    assert proof.geofence_clear and proof.terrain_margin_m > 0 and proof.obstacle_margin_m > 0
    if command == "loiter_at":
        hold = frame.hold_sites["hold:orbit"]
        assert receipt["status"] == "accepted_unconfirmed"
        assert commands == [("loiter_at", (hold.lat_deg, hold.lon_deg), 60, 180, 15)]
        assert proof.required_time_s == 90
    else:
        assert receipt["status"] == "sent_unconfirmed" and commands == [command]
    if decision["action"] == "return":
        assert proof.landing_site_clear and proof.required_time_s == 110


@pytest.mark.parametrize(("decision", "fw", "mutate", "reason"), [
    ({"action": "return", "params": {}}, True, None, "unverified_clearance"),  # No FW native RTL bound.
    ({"action": "land-now", "params": {}}, False, None, "unverified_clearance"),  # 205 m from the pad.
    ({"action": "loiter", "params": {"hold_ref": "hold:mc"}}, False,
     lambda e: e["native"]["holds"]["hold:mc"].update(max_entry_speed_mps=4), "unverified_clearance"),
    # A FW orbit is not offered while the vehicle is in MC.
    ({"action": "loiter", "params": {"hold_ref": "hold:orbit"}}, False, None, "hold_ref_not_offered"),
    ({"action": "loiter", "params": {"hold_ref": "hold:orbit"}}, True,
     lambda e: e["native"]["holds"]["hold:orbit"].update(radius_m=20, padding_ne_m=70), "orbit_radius_limit"),
    ({"action": "return", "params": {}}, False,
     lambda e: e["native"]["return"]["3"]["legs"][-1].update(end=[4, 0, 100]), "unverified_clearance"),
])
def test_live_native_proofs_fail_closed(terrain, profile, monkeypatch, decision, fw, mutate, reason):  # noqa: F811
    receipt, _built, commands = _exercise_native(terrain, profile, monkeypatch, decision, fw=fw, mutate=mutate)
    assert (receipt["action"], receipt["status"], receipt["reason"]) == (decision["action"], "rejected", reason)
    assert commands == []


@pytest.mark.parametrize(("change", "match"), [
    (lambda n: n["return"].update({"1": n["return"]["3"]}), "MC return step"),
    (lambda n: n["land-now"].update({"1": n["land-now"]["3"]}), "MC return step"),
    (lambda n: n["holds"]["hold:orbit"].update(padding_ne_m=10), "whole disk"),
    (lambda n: n["holds"]["hold:mc"].update(mode="glide"), "mc or fw"),
    (lambda n: n["holds"]["hold:mc"].update(steps=[4]), "step indices"),
    (lambda n: n.update(extra={}), "sections"),
])
def test_native_bound_schema_is_strict(terrain, change, match):  # noqa: F811
    from multidetect.sim_teacher.x1_live_clearance import parse_native

    native = _native_bounds(fw=True)
    parse_native(native, terrain)
    change(native)
    with pytest.raises(ValueError, match=match):
        parse_native(native, terrain)


def test_continue_still_needs_its_own_entry_gate(terrain, profile, monkeypatch):  # noqa: F811
    # A hold entry that admits a faster vehicle must not widen the continue entry limit.
    def mutate(evidence):
        evidence["suffixes"]["3"]["max_entry_speed_mps"] = 4
        evidence["native"]["holds"]["hold:mc"]["max_entry_speed_mps"] = 12

    receipt, _built, commands = _exercise_native(terrain, profile, monkeypatch, {"action": "continue", "params": {}},
                                                 mutate=mutate)
    assert (receipt["status"], receipt["reason"]) == ("rejected", "unverified_clearance") and commands == []


def test_assumed_native_bounds_are_scoped_labelled_and_parse_on_the_iowa_dem():
    from multidetect.sim_teacher.x1_live_clearance import parse_native
    from multidetect.terrain_dem import load_local_dem_grid
    from scripts import generate_x1_assumed_sim_bounds as generator
    from scripts import run_x1_autonomy_demo as demo

    raw = json.loads((demo.ROOT / "configs/simulation/x1-assumed-sim-bounds-iowa.json").read_text(encoding="utf-8"))
    entries = [raw["px4_native_parameters"], *(e for section in raw["native"].values() for e in section.values())]
    assert all(e["basis"] == "ASSUMED_SIM" and len(e["justification"].splitlines()) == 1 for e in entries)
    assert set(raw["native"]["return"]) == {"3"}  # FW native RTL stays unmodelled and rejected.
    assert generator.scopes_for("link_loss") == {generator.SCOPES["link_loss"], generator.SCOPES["link_loss:mc_return"]}
    assert generator.scopes_for("battery_drop") == {generator.SCOPES["battery_drop"]}
    assert generator.scopes_for(None) == set()
    iowa = load_local_dem_grid(demo.ROOT / "simulation/terrain/rw_farm_iowa_w6km__clear_noon.dem.json")
    native = {section: {k: v["value"] for k, v in items.items()} for section, items in raw["native"].items()}
    returns, landings, holds = parse_native(native, iowa)
    assert set(returns) == {3} and set(landings) == {3}
    orbit = holds["hold:yard-orbit"]
    assert orbit.site.mode == "fw" and orbit.site.radius_m == 80 and orbit.steps == {1}
    assert iowa.local_from_wgs84(orbit.site.lat_deg, orbit.site.lon_deg) == pytest.approx((320, 0), abs=0.05)
    assert holds["hold:mc-here"].site == HoldSite("mc")


def test_finish_flight_scores_a_confirmed_native_return(tmp_path, terrain, profile):  # noqa: F811
    from scripts import run_x1_autonomy_demo as demo

    package = _package(plan=_plan(), capabilities=["gimbal_control"])
    steps = _build(package, terrain, profile)
    names = [step for step in steps if step.command is not None]
    expected = {"reposition": "arrived", "transition": "transitioned", "mc_reposition": "settled"}
    rows = [{"labels": {"motion": step.command.to_dict()}, "outcome": {"status": "success"},
             "ack": {"motion_wait": {"waited": expected[step.command.kind], "x1_passage": True}}}
            for step in names[:3]]
    (tmp_path / "steps.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    (tmp_path / "episode.json").write_text('{"status": "complete"}', encoding="utf-8")
    pad = tmp_path / "mission-package.json"
    pad.write_text(json.dumps({"x1_live_evidence": {"landing_pad": {"radius_m": 16, "elevation_error_m": 13}}}),
                   encoding="utf-8")

    def flight(final_east, status="confirmed"):
        mavlink = [
            {"message_type": "LOCAL_POSITION_NED", "x": 0, "y": 0, "z": 0},
            {"message_type": "fixed_wing_takeoff_complete"},
            {"message_type": "collector_route_superseded", "step_index": 3, "phase": "during_step"},
            {"message_type": "scenario_native_command", "action": "return", "command": 20},
            {"message_type": "scenario_native_handoff", "action": "return", "status": status, "reason": "mode_observed"},
            {"message_type": "LOCAL_POSITION_NED", "x": 0, "y": final_east, "z": 0},
            {"message_type": "collector_landing_complete", "method": "native_return",
             "flight_state": {"landed_state": 1, "vtol_state": 3}},
        ]
        (tmp_path / "mavlink.jsonl").write_text("".join(json.dumps(row) + "\n" for row in mavlink), encoding="utf-8")
        record = {"flight": {}, "markers": [], "safety": {"observed": {}},
                  "contingency_scenario": {"live_evidence": {"package_path": str(pad)}}}
        return demo.finish_flight(tmp_path, steps, record), record["flight"]

    passed, summary = flight(3.0)
    assert passed and summary["return_home"] and summary["landing"]
    assert summary["native_handoff"]["superseded_steps"] == ["mc_return_to_home"]
    assert summary["native_handoff"]["landing_offset_from_first_sample_m"] == 3.0
    assert flight(30.0)[0] is False  # Landed outside the surveyed pad: not a return home.
    assert flight(3.0, status="unconfirmed")[0] is False
