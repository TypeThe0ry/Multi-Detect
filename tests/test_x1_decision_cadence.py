"""CPU-only cadence checks. The replay port cannot send to a vehicle."""
from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import pytest

from multidetect.autonomy.x1_contingency_executor import CADENCE_EVENT_ID_BASE, MAX_DECISION_AGE_MS
from multidetect.autonomy.x1_contingency_state import mission_navigation
from multidetect.sim_teacher.x1_scenario import CollectorScenario
from multidetect.sim_teacher.x1_scenario_decision import DecisionTick, recorded_decisions
from test_x1_motion_proof import _frame, _native_fixture, _sweep, admitted  # noqa: F401
from test_x1_mission import profile, terrain  # noqa: F401

CONTINUE = {"action": "continue", "params": {}}
HOLD = {"action": "loiter", "params": {"hold_ref": "here"}}
RETURN = {"action": "return", "params": {}}
EVENT = (("target_lost", "tracker"),)


class Replay:
    def __init__(self, admitted, profile, backend, *, interval=20):
        package, _, builder = admitted
        self.start = self.now = 1000
        self.frame = _frame(package)
        self.markers, self.requests, self.proofs = [], [], []
        self.phase = "approach"
        self.mode = (129, 1234, 2)
        self.active = True

        def post(request):
            self.requests.append(request)
            return backend(request)

        def prove(decision, frame):
            _, hold_sweep = _native_fixture("hold", package)
            sweep = hold_sweep(decision) if decision.action == "loiter" else _sweep(decision, frame)
            proof = builder.build(decision, frame, sweep)
            self.proofs.append((decision.action, proof))
            return proof

        self.tick = DecisionTick(package, profile, post, None, self.markers.append, run_id="cadence-cpu",
                                 plan_digest="a" * 64, now_ms=lambda: self.now, cpu_replay=True,
                                 decision_cadence_s=interval, build_proof=prove)

    def at(self, elapsed_ms, **kwargs):
        self.now = self.start + elapsed_ms
        progress = {"phase": self.phase, "step_index": self.frame.active_motion_index,
                    "step_id": "finish", "remaining_plan_sha256": "b" * 64,
                    "next_waypoint_distance_m": 410, "home_distance_m": 410, "eta_s": 82,
                    "remaining_energy_wh": 60, "remaining_time_s": 300, "hold": {"active": False}}
        self.tick.tick(replace(self.frame, t_ms=self.now), mission_progress=progress, mode=self.mode,
                       cadence_active=self.active, **kwargs)

    @property
    def receipts(self):
        return [row for row in self.markers if row["message_type"] == "scenario_decision_receipt"]

    def reply(self):
        return self.tick.future.result(timeout=2)


@pytest.fixture
def replay(admitted, profile):  # noqa: F811
    instances = []

    def make(backend, **kwargs):
        run = Replay(admitted, profile, backend, **kwargs)
        instances.append(run)
        return run

    yield make
    for run in instances:
        run.tick.close()


def test_ninety_second_flight_continue_then_exact_hold(replay):
    run = replay(recorded_decisions([CONTINUE] * 9 + [HOLD]))
    for start in range(0, 90_001, 10_000):
        run.at(start)
        run.reply()
        run.at(start + 25)
        if start < 90_000:
            assert run.tick.replay_commands == []
    assert run.tick.replay_commands == [("set_mode", "HOLD")]
    assert run.tick.executor.native_owner == "loiter"
    assert [action for action, _ in run.proofs] == ["continue"] * 9 + ["loiter"]
    assert len(run.markers) == len(run.receipts) == 10
    assert [row["status"] for row in run.receipts] == ["continued"] * 9 + ["sent_unconfirmed"]
    assert all(row["trigger"] == "cadence" and row["latency_ms"] == 25 for row in run.receipts)
    assert all("wire" not in row and "motion_proof" not in row for row in run.receipts[:-1])
    for n, request in enumerate(run.requests, 1):
        assert request["schema"] == "x1.contingency.request.v1"
        assert request["event_id"] == CADENCE_EVENT_ID_BASE + n
        assert request["deadline_ms"] - request["state"]["t_ms"] == MAX_DECISION_AGE_MS
        assert request["state"]["latest_event_id"] == 0 and request["state"]["events"] == []
        assert request["remaining_plan_sha256"] == "b" * 64
    progress = run.requests[-1]["mission_progress"]
    assert [(row["request_id"], row["status"]) for row in progress["recent_receipts"]] == [
        (f"cadence-cpu:{n}", "continued") for n in range(6, 10)]
    assert progress["step_id"] == "finish" and progress["hold"]["active"] is False


@pytest.mark.parametrize("change", ["step", "mode", "revision"])
def test_ready_cadence_response_is_rejected_when_context_changes(replay, change):
    run = replay(recorded_decisions([RETURN]))
    run.at(0)
    run.reply()
    if change == "step":
        run.frame = replace(run.frame, active_motion_index=2)
    elif change == "mode":
        run.mode = (129, 5678, 2)
    else:
        run.frame = replace(run.frame, revision=2)
    run.at(10)
    assert run.receipts[-1]["reason"] == f"cadence_{change}_changed"
    assert run.tick.replay_commands == [] and run.proofs == []


def test_deadline_revokes_ticket_while_backend_is_running(replay):
    started, release = Event(), Event()
    recorded = recorded_decisions([RETURN])

    def slow(request):
        started.set()
        assert release.wait(2)
        return recorded(request)

    run = replay(slow)
    run.at(0)
    assert started.wait(2)
    try:
        run.at(MAX_DECISION_AGE_MS + 1)
        assert run.receipts[-1]["reason"] == "decision_timeout"
        assert not run.tick.future.done()
    finally:
        release.set()
    run.reply()
    run.at(MAX_DECISION_AGE_MS + 10)
    assert len(run.receipts) == 1 and run.tick.replay_commands == [] and run.proofs == []


@pytest.mark.parametrize("invalid", ["{broken", "null", "\ud800", {"action": "return"}])
def test_invalid_output_is_a_no_action_receipt(replay, invalid):
    run = replay(lambda _: invalid)
    run.at(0)
    run.reply()
    run.at(10)
    assert run.receipts[-1]["reason"] == "invalid_decision"
    assert run.tick.replay_commands == [] and run.proofs == []


def test_event_within_deadline_preempts_cadence_and_runs_next(replay):
    started, release = Event(), Event()
    recorded = recorded_decisions([RETURN, HOLD])

    def controlled(request):
        if request.get("trigger") == "cadence":
            started.set()
            assert release.wait(2)
        return recorded(request)

    run = replay(controlled)
    run.at(0)
    assert started.wait(2)
    try:
        run.at(20, events=EVENT)
        assert run.receipts[-1]["reason"] == "preempted_by_event"
        assert len(run.requests) == 1  # No overlapping call, even during preemption.
    finally:
        release.set()
    run.reply()
    run.at(30)
    run.reply()
    run.at(40)
    assert [req["request_id"] for req in run.requests] == ["cadence-cpu:1", "cadence-cpu:2"]
    assert "trigger" not in run.requests[1]  # Event request wire shape is preserved.
    assert run.requests[1]["event_id"] == run.requests[1]["state"]["latest_event_id"] == 1
    assert run.tick.replay_commands == [("set_mode", "HOLD")]
    assert [action for action, _ in run.proofs] == ["loiter"]
    assert [(row["trigger"], row["status"]) for row in run.receipts] == [
        ("cadence", "rejected"), ("event", "sent_unconfirmed")]


def test_rate_limits_by_phase_uses_floor_and_never_catches_up(replay):
    run = replay(recorded_decisions([CONTINUE] * 5), interval=0.01)
    timeline = [(0, "takeoff"), (500, "transition"), (1500, "cruise"), (2000, "approach"), (90_000, "approach")]
    for when, phase in timeline:
        run.phase = phase
        if when:
            run.at(when - 1 if when != 90_000 else 2200)
            assert run.tick.future is None
        run.at(when)
        run.reply()
        run.at(when + 1)
    assert [r["state"]["t_ms"] - run.start for r in run.requests] == [0, 500, 1500, 2000, 90_000]
    assert [r["mission_progress"]["phase"] for r in run.requests] == [p for _, p in timeline]


def test_cadence_defaults_off_and_events_still_use_the_executor(replay):
    run = replay(recorded_decisions([CONTINUE]), interval=0)
    run.at(0)
    run.at(90_000)
    assert run.tick.future is None
    run.at(90_001, events=EVENT)
    run.reply()
    run.at(90_010)
    assert len(run.receipts) == 1 and run.receipts[0]["status"] == "continued"
    assert run.tick.replay_commands == []


def test_progress_uses_horizontal_distance_and_closing_speed():
    assert mission_navigation({"x": 100, "y": 0, "vx": 5, "vy": 0}, (400, 0, -80)) == (300, 100, 60)
    assert mission_navigation({"x": 100, "y": 0, "vx": 5, "vy": 0}, (0, 0, -80)) == (100, 100, None)
    assert mission_navigation({"x": 100, "y": 0, "vx": "5", "vy": 0}, (400, 0, -80)) == (300, 100, None)
    assert mission_navigation(None, (400, 0, -80)) == (None, None, None)


def test_collector_progress_follows_compiled_lifecycle_and_reports_hold(admitted, monkeypatch):
    package, steps, _ = admitted
    scenario = CollectorScenario((), clearance=None, cadence_steps=steps)
    scenario.decision = SimpleNamespace(plan_digest="a" * 64)
    monkeypatch.setattr(scenario, "_fresh_sample", lambda *args, **kw: {"x": 100, "y": 0, "vx": 5, "vy": 0})
    frame = _frame(package)
    takeoff = scenario._mission_progress(0, "takeoff", None, frame)
    transition = scenario._mission_progress(0, None, None, frame)
    cruise = scenario._mission_progress(1, None, None, replace(frame, vtol_state="fw"))
    landing = scenario._mission_progress(3, "approach", None, frame)
    assert [p["phase"] for p in (takeoff, transition, cruise, landing)] == [
        "takeoff", "transition", "cruise", "approach"]
    assert [p["step_index"] for p in (takeoff, transition, cruise, landing)] == [0, 1, 2, 5]
    assert len({p["remaining_plan_sha256"] for p in (takeoff, transition, cruise, landing)}) == 4
    assert (cruise["next_waypoint_distance_m"], cruise["home_distance_m"], cruise["eta_s"]) == (300, 100, 60)
    assert landing["eta_s"] is None
    scenario.hold = {"hold_ref": "here", "deadline_ms": frame.t_ms + 5000}
    scenario.native = SimpleNamespace(action="loiter", status="confirmed")
    progress = scenario._mission_progress(3, None, None, frame)
    assert progress["phase"] == "hold"
    assert progress["hold"] == {"active": True, "ref": "here", "remaining_s": 5,
                                "native_action": "loiter", "native_status": "confirmed"}
