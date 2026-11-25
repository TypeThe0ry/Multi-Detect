"""Collector-side Altair request and receipt plumbing for X1 SITL."""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from types import SimpleNamespace
from urllib.request import Request, urlopen

from multidetect.autonomy.x1_contingency_executor import (
    DECISION_SCHEMA,
    MAX_DECISION_BYTES,
    X1AirbornePlanAdapter,
    X1ContingencyExecutor,
    X1Envelope,
)
from multidetect.autonomy.x1_contingency_state import X1StateBuilder


def llama_decisions(port, sentence):
    def post(request):
        body = {"model": "local", "temperature": 0, "max_tokens": 512, "messages": [
            {"role": "system", "content": (
                f"You are the onboard Altair planner. Return one {DECISION_SCHEMA} JSON object with "
                "schema, request_id, event_id, mission_id, revision, action and params. Echo the current "
                "IDs from the request and state. Actions: continue/return/land-now/abort-transition with "
                "empty params, loiter with hold_ref, retask with plan, gimbal-retarget with pitch_deg and "
                "yaw_deg. Select only a defensible action given the observations and available evidence.")},
            {"role": "user", "content": json.dumps({"task": sentence, "request": request})},
        ]}
        req = Request(f"http://127.0.0.1:{port}/v1/chat/completions", json.dumps(body).encode(),
                      {"Content-Type": "application/json"})
        with urlopen(req, timeout=2.5) as response:
            wire = json.loads(response.read(65_536))["choices"][0]["message"]["content"]
        return wire
    return post


def recorded_decisions(decisions):
    """Yield recorded actions while binding IDs to the current CPU fixture request."""
    choices = iter(decisions)

    def post(request):
        decision = next(choices)
        state = request["state"]
        return json.dumps({**decision, "schema": DECISION_SCHEMA, "request_id": request["request_id"],
                           "event_id": request["event_id"], "mission_id": state["mission_id"],
                           "revision": state["revision"]})
    return post


class _ReplayPort:
    """Record dispatcher calls in memory; this port cannot reach a vehicle."""

    def __init__(self):
        self.commands = []

    def rtl(self):
        self.commands.append(("rtl",))

    def land(self):
        self.commands.append(("land",))

    def set_mode(self, mode):
        self.commands.append(("set_mode", mode))

    def loiter_at(self, centre, radius, altitude, *, speed):
        self.commands.append(("loiter_at", centre, radius, altitude, speed))
        return SimpleNamespace(ok=True, ack_result=0)  # Synthetic ACK, never a readback.

    def gimbal_control(self, *, pitch_deg, yaw_deg):
        self.commands.append(("gimbal_control", pitch_deg, yaw_deg))
        return SimpleNamespace(ok=True, ack_result=0)


class DecisionTick:
    """Submit on a worker. Validate and apply on the serialized collector tick."""

    def __init__(self, package, profile, backend, gimbal_port, record, *, run_id, plan_digest,
                 revision=1, peer=(1, 1), now_ms=lambda: time.monotonic_ns() // 1_000_000,
                 build_proof=None, cpu_replay=False):
        if cpu_replay and gimbal_port is not None:
            raise ValueError("CPU replay cannot take a live port")
        replay_port = _ReplayPort() if cpu_replay else None
        self.replay_commands = replay_port.commands if replay_port is not None else None
        self.builder = X1StateBuilder(vehicle_system=peer[0], vehicle_component=peer[1])
        self.executor = X1ContingencyExecutor(
            X1AirbornePlanAdapter(revision, replace_remaining=lambda *_: False),
            replay_port if replay_port is not None else gimbal_port,
            package, X1Envelope(profile), claim_native=lambda: replay_port is not None,
            capabilities=frozenset(package.document["capabilities"]),
        )
        self.mission_id = package.mission_id
        self.revision = revision
        self.backend, self.record, self.now_ms = backend, record, now_ms
        self.run_id, self.plan_digest = run_id, plan_digest
        self.build_proof = build_proof
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="x1-altair-decision")
        self.future = None
        self.last_requested = 0

    def _mark(self, phase, **fields):
        self.record({"message_type": f"scenario_decision_{phase}",
                     "receive_monotonic_ns": time.monotonic_ns(), **fields})

    def tick(self, frame, *, mav_rows=(), observations=(), events=(), event_current=True, wind=None):
        now = self.now_ms()
        for row in sorted(mav_rows, key=lambda row: row["receive_monotonic_ns"]):
            stamp = row["receive_monotonic_ns"] // 1_000_000
            # The builder clock includes our previous snapshot. Admit each new
            # sample once and only if it arrived after that snapshot.
            if getattr(self, "last_tick_ms", 0) < stamp <= now:
                self.builder.ingest_mavlink(stamp, row)
        for group, values, source in observations:
            self.builder.observe(now, group, values, source=source)
        for event, source in events:
            confidence = wind.confidence if event == "wind_step" and wind is not None else None
            self.builder.event(now, event, source=source, confidence=confidence)
        snapshot = self.builder.snapshot(now, mission_id=self.mission_id, revision=self.revision)
        if wind is not None:
            # This tick's selected reading already carries its own kinematic timestamp.
            snapshot["telemetry"]["wind"] = wind.telemetry(now)
        self.last_tick_ms = now
        event_id = snapshot["latest_event_id"]
        if self.future is not None and self.future.done():
            try:
                wire = self.future.result()
                if isinstance(wire, str) and len(wire.encode()) > MAX_DECISION_BYTES:
                    wire = ""
                current_id = event_id if event_current else 0
                # A reference must still denote the same hold option that was offered.
                holds = {ref: site for ref, site in frame.hold_sites.items() if self.offered_holds.get(ref) == site}
                admission = self.executor.apply(wire, replace(frame, event_id=current_id, hold_sites=holds),
                                                now_ms=now, build_proof=self.build_proof)
            except Exception as exc:
                self._mark("error", request_id=self.request_id, error=type(exc).__name__)
            else:
                self._mark("receipt", request_id=self.request_id, wire=wire if isinstance(wire, str) else None,
                           **asdict(admission))
            finally:
                self.future = None
        if self.future is None and event_current and snapshot["events"] and event_id != self.last_requested:
            self.last_requested = event_id
            self.offered_holds = dict(frame.hold_sites)
            request = self.executor.open_request(snapshot, run_id=self.run_id,
                                                 hold_refs=tuple(self.offered_holds),
                                                 remaining_plan_sha256=self.plan_digest)
            self.request_id = request["request_id"]
            self._mark("request", request=request)
            self.future = self.pool.submit(self.backend, request)

    def close(self):
        if self.future is not None:
            self._mark("discarded", request_id=self.request_id, reason="collector_exit")
        self.pool.shutdown(wait=False, cancel_futures=True)


MAVLINK_TYPES = ("BATTERY_STATUS", "WIND_COV", "SYS_STATUS", "HEARTBEAT", "GPS_RAW_INT",
                 "ESTIMATOR_STATUS", "EXTENDED_SYS_STATE", "VFR_HUD", "TERRAIN_REPORT", "MISSION_CURRENT")


class GimbalIngress:
    """Reuse the collector's existing single gimbal writer and readback loop."""

    hold = None

    def gimbal_control(self, *, pitch_deg, yaw_deg):
        hold = self.hold
        if hold is None or hold.awaiting_readback or hold.session.poll() not in {"idle", "ready"}:
            return SimpleNamespace(ok=False, ack_result=1)
        previous_target, previous_stamp = hold.target, hold.sent_vehicle_ms
        hold.target = {"pitch": pitch_deg, "yaw": yaw_deg, "roll": 0.0}
        hold.sent_vehicle_ms = None
        try:
            hold.tick()
        finally:
            if not hold.awaiting_readback:
                hold.target, hold.sent_vehicle_ms = previous_target, previous_stamp
        return SimpleNamespace(ok=hold.awaiting_readback, ack_result=None)
