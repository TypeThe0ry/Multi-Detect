#!/usr/bin/env python3
"""Build CPU-only X1 flight-loop SFT and frozen holdout prompts from synthetic episodes."""

from __future__ import annotations

import argparse
import hashlib
import math
import random
from collections import Counter, deque
from dataclasses import asdict, dataclass, replace
from functools import cache
from pathlib import Path
from types import SimpleNamespace

from multidetect.autonomy.x1_contingency_executor import (
    DECISION_SCHEMA,
    CommandReceipt,
    HoldSite,
    MotionProof,
    RuntimeFrame,
    X1AirbornePlanAdapter,
    X1ContingencyExecutor,
    X1Envelope,
)
from multidetect.autonomy.x1_contingency_state import X1StateBuilder
from multidetect.autonomy.x1_mission import build_x1_mission_steps, x1_place_from_wgs84
from multidetect.autonomy.x1_perception_tools import (
    TOOL_SCHEMA,
    BatteryReading,
    InjectedTargetReading,
    PerceptionSources,
    RouteReading,
    ToolRunner,
)
from multidetect.sim_teacher.x1_live_clearance import LiveContinueEvidence
from multidetect.sim_teacher.x1_retask import AirborneRouteSlot

from scripts.run_x1_autonomy_demo import load_sim_replay_package
from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.motion_intent import resolve_airframe_profile
from multidetect.autonomy.serialization import canonical_json, canonical_sha256, strict_json_loads
from multidetect.target_geolocation import wgs84_forward_from_ned
from multidetect.terrain_dem import LocalDemGrid

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "x1.flight-loop-sft.v1"
SPLITS = {"train": (42619, 12), "val": (81023, 3), "holdout": (95147, 4)}
CAPS = frozenset({"airframe:x1"})
ADMITTED = frozenset({"continued", "sent_unconfirmed", "accepted_unconfirmed", "installed"})
SYSTEM = (
    f"You are the onboard Altair planner. Return one {DECISION_SCHEMA} JSON object with schema, "
    "request_id, event_id, mission_id, revision, action and params. Echo the current IDs from the "
    "request and state (event_id comes from the request, not latest_event_id). Actions: "
    "continue/return/land-now/abort-transition with empty params, loiter with hold_ref, "
    "retask with plan, gimbal-retarget with pitch_deg and yaw_deg. Select only a defensible action "
    "given the observations and available evidence. If you need one more observation and "
    f"perception.tools lists its name, return one {TOOL_SCHEMA} JSON object with schema, tool, "
    "args={}, request_id and call_index (echo perception.call_index). The next request has the "
    "same state and ordered tool results. Treat ok=false as unavailable, never as a negative "
    "observation. When the evidence is sufficient or no calls remain, return an action object."
)
TASKS = {
    "train": (
        "Inspect survey marker {n} from the northbound transect, then return and land.",
        "沿向北测线观察测绘标记 {n}，完成后返航降落。",
    ),
    "val": (
        "Follow the northbound survey pass to inspect marker {n} before landing back at launch.",
        "沿北向航段查看测绘标记 {n}，随后飞回起飞点落地。",
    ),
    "holdout": (
        "Check marker {n} on the northbound mapping run and finish at the launch pad.",
        "执行北向测绘时查看标记 {n}，最终在起飞坪落地。",
    ),
}
PHASES = {
    "climb": (0, "takeoff", "vtol_takeoff", "mc"),
    "transition": (1, "transition", "fw_transition", "front_transition"),
    "cruise": (2, "cruise", "fw_goto_waypoint", "fw"),
    "approach": (4, "approach", "mc_return_to_home", "mc"),
}
FOCUS = {
    "nominal": None,
    "target_found": "cruise",
    "target_lost": "cruise",
    "target_moved": "cruise",
    "tool_budget": "cruise",
    "stale_target": "cruise",
    "energy_return": "cruise",
    "battery_unavailable": "cruise",
    "battery_land": "approach",
    "wind_hold": "cruise",
    "wind_mild": "cruise",
    "link_return": "cruise",
    "transition_no_override": "transition",
    "px4_owns": "approach",
}


def jsonl(path, rows):
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(canonical_json(row) + "\n")


def _result(request, tool):
    return next(
        (
            row["data"]
            for row in reversed(request["perception"]["results"])
            if row["tool"] == tool and row["ok"] and row["age_ms"] <= 1000
        ),
        None,
    )


def _identity(request):
    return {
        "request_id": request["request_id"],
        "event_id": request["event_id"],
        **{key: request["state"][key] for key in ("mission_id", "revision")},
    }


def _follow_plan():
    # #72's airborne skill graph: return_home lowers to MC return and terminal land.
    target = {"target_ref": "finding:1", "altitude_agl_m": 85}
    steps = [
        ("approach", "fw_goto_waypoint", target, "observe"),
        ("observe", "loiter_observe", {**target, "radius_m": 65, "duration_s": 14}, "transition"),
        ("transition", "back_transition", {}, "home"),
        ("home", "return_home", {}, None),
    ]
    return {
        "plan_version": 1,
        "goal": "Observe the located survey marker, then return and land.",
        "report": [],
        "steps": [
            {
                "id": name,
                "skill": skill,
                "params": params,
                "on_success": nxt,
                "on_fail": "home" if nxt else None,
            }
            for name, skill, params, nxt in steps
        ],
    }


def policy(request):
    """Synthetic pilot oracle. Its only episode-dependent input is the on-wire request."""
    perception = request["perception"]
    attempted = {row["tool"] for row in perception["results"]}
    state = request["state"]
    tel = state["telemetry"]
    mode = (tel["vtol"] or {}).get("state")
    nav = tel["navigation"] or {}
    battery = tel["battery"] or {}
    target = tel["target"] or {}
    events = {row["type"] for row in state["events"] if state["t_ms"] - row["t_ms"] <= 3000}

    def ask(*names):
        for tool in names:
            if tool in perception["tools"] and tool not in attempted:
                return {
                    "schema": TOOL_SCHEMA,
                    "tool": tool,
                    "args": {},
                    "request_id": request["request_id"],
                    "call_index": perception["call_index"],
                }
        return None

    def decision(action="continue", **params):
        return {"schema": DECISION_SCHEMA, **_identity(request), "action": action, "params": params}

    # No new host maneuver during a transition or FC emergency. The executor can
    # still deny continue; that means no command, not a fallback chosen by the host.
    if mode not in {"mc", "fw"} or (tel["fc"] or {}).get("system_status") in {5, 6}:
        return decision()

    energy = nav.get("remaining_energy_wh")
    pct = battery.get("pct")
    low = (energy is not None and energy < 40) or (pct is not None and pct <= 25)
    if low:
        if observe := ask("battery_status"):
            return observe
        bounded = _result(request, "battery_status")
        if bounded is not None:
            lower = bounded["remaining_wh"] - bounded["absolute_error_wh"]
            distance = nav.get("home_distance_m")
            if (
                mode == "mc"
                and 4 <= lower < 24
                and distance is not None
                and distance <= 12
                and (tel["terrain"] or {}).get("conflict") is False
            ):
                return decision("land-now")
            if 28 <= lower < 40:
                return decision("return")

    link = tel["link"] or {}
    if (
        link.get("gcs") == "down"  # link_loss_policy return_now: no grace window
        and energy is not None
        and energy >= 28
    ):
        return decision("return")

    wind = tel["wind"] or {}
    if (
        mode == "fw"
        and "hold:survey-orbit" in request["hold_refs"]
        and energy is not None
        and energy >= 40
    ):
        if math.hypot(wind.get("north_mps") or 0, wind.get("east_mps") or 0) >= 8:
            if observe := ask("terrain_ahead", "route_status"):
                return observe
            terrain, route = (_result(request, tool) for tool in ("terrain_ahead", "route_status"))
            if (
                terrain is not None
                and 0 <= terrain["min_sample_margin_m"] <= 60
                and route
                and route["steps"]
            ):
                return decision("loiter", hold_ref="hold:survey-orbit")

    if mode == "fw" and (target.get("visible") is True or "target_lost" in events):
        if observe := ask("look", "target_status"):
            return observe
        look, located = (_result(request, tool) for tool in ("look", "target_status"))
        if (
            look
            and located
            and located["visible"]
            and look.get("track_id") == located.get("track_id")
        ):
            if observe := ask("route_status"):
                return observe
            route = _result(request, "route_status")
            progress = request.get("mission_progress", {})
            detections = look["detections"]
            if (
                events & {"target_found", "target_reacquired"}
                and route
                and route["steps"]
                and route["steps"][0]["primitive"] == "fw_goto_waypoint"
                and progress.get("phase") == "cruise"
                and (state["host_leg"] or {}).get("progress") == 1
                and target.get("track_ref") == located["track_id"]
                and located["error_bound_m"] <= 12
                and energy is not None
                and energy >= 55
                and nav.get("remaining_time_s", 0) >= 300
                and any(
                    row["class"] == "survey_marker" and abs(row["bearing_deg"]) >= 20
                    for row in detections
                )
            ):
                return decision("retask", plan=_follow_plan())
    return decision()


@cache
def _inputs(root):
    simulation = root / "configs/simulation"

    def read(name):
        return strict_json_loads((simulation / name).read_bytes())

    scenario = {
        row["type"]: row["values"]
        for name in (
            "x1-contingency-faults-v1.json",
            "x1-evidence-follow-faults-v1.json",
            "x1-native-handoff-faults-v1.json",
        )
        for row in read(name)["faults"]
    }
    return (
        resolve_airframe_profile("delta_tailsitter_gimbal", base_dir=root),
        read("x1-assumed-sim-bounds-iowa.json"),
        scenario,
    )


class Replay:
    """Private episode state and a command recorder, never serialized into messages."""

    def __init__(self, root, run_id, task, origin):
        self.profile, assumptions, self.scenario = _inputs(root)
        self.run_id, self.task = run_id, task
        dem = ((100.0,) * 21,) * 21
        digest = canonical_sha256({"origin": origin, "elevations": dem})
        self.terrain = LocalDemGrid(
            "x1-flight-loop-cpu",
            *origin,
            100.0,
            -600.0,
            -600.0,
            100.0,
            100.0,
            dem,
            0.1,
            0.0,
            digest,
        )
        scope = canonical_sha256({"case": run_id, "dem": digest, "bounds": assumptions})
        area = {"north_min_m": -580, "north_max_m": 1100, "east_min_m": -570, "east_max_m": 950}
        suffixes = {
            key: {**entry["value"], "entry": [*entry["value"]["entry"][:4], 175, 195]}
            for key, entry in assumptions["suffixes"].items()
        }
        self.package = load_sim_replay_package(
            {
                "mission_id": run_id,
                "home": [0, 0],
                "places": {"poi:transect": [320, 0]},
                "zones": {},
                "routes": {},
                "task_text": task,
                "capabilities": sorted(CAPS),
                "risk_level": "low",
                "oversight_steps": [],
                "link_loss_policy": {"action": "return_now", "timeout_s": 20},
                "operating_area": {**area, "min_altitude_m": 15, "max_altitude_m": 145},
                "budget": {
                    "time_s": 1000,
                    "reserve_time_s": 100,
                    "energy_wh": 72,
                    "reserve_energy_wh": 14.8,
                    "battery_capacity_wh": 72,
                },
                "x1_planning": {
                    "world": "rw_x1_flight_loop_cpu",
                    "max_distance_from_home_m": 1200,
                    "corridor": area,
                    "terrain_rel_home_m": [-25, 25],
                },
                "x1_live_evidence": {
                    "basis": "ASSUMED_SIM",
                    "assumed_bounds_sha256": scope,
                    "scope_sha256": scope,
                    "dem_sha256": digest,
                    "dem_error_m": 1,
                    "obstacle_coverage": [-580, 1200, -570, 960, 90, 220],
                    "obstacles": [],
                    "landing_pad": {"radius_m": 16, "elevation_error_m": 8},
                    "battery": {
                        "max_voltage_v": 17,
                        "soc_error_pct": 1,
                        "consumption_error_wh": 0.25,
                    },
                    "suffixes": suffixes,
                },
            },
        )
        plan = validate_mission_plan(
            {
                "plan_version": 1,
                "goal": task,
                "report": [],
                "steps": [
                    {
                        "id": "transect",
                        "skill": "goto",
                        "params": {"target_ref": "poi:transect", "altitude_m": 85},
                        "on_success": "home",
                        "on_fail": "home",
                    },
                    {
                        "id": "home",
                        "skill": "return_home",
                        "params": {},
                        "on_success": None,
                        "on_fail": None,
                    },
                ],
            }
        )
        self.steps = build_x1_mission_steps(
            self.package,
            plan=plan,
            terrain=self.terrain,
            profile=self.profile,
            mission_radius_m=1200,
        )
        self.plan_hash = canonical_sha256(asdict(plan))
        self.evidence = LiveContinueEvidence(
            self.package,
            self.profile,
            self.terrain,
            self.steps,
            scope=scope,
            assumed_sim={
                "sha256": scope,
                "capacity_wh": 72,
                "airborne_retask": assumptions["airborne_retask"]["value"],
            },
        )
        self.envelope = X1Envelope(self.profile)

    def executor(self, route, commands):
        def record(*args, **kwargs):
            commands.append((args, kwargs))

        def acknowledge(*args, **kwargs):
            record(*args, **kwargs)
            return CommandReceipt(True, 0)

        vehicle = SimpleNamespace(
            rtl=record,
            land=record,
            set_mode=record,
            loiter_at=acknowledge,
            vtol_transition=acknowledge,
            gimbal_control=acknowledge,
        )
        return X1ContingencyExecutor(
            X1AirbornePlanAdapter(1, replace_remaining=route.replace_remaining),
            vehicle,
            self.package,
            self.envelope,
            claim_native=lambda: True,
            capabilities=CAPS,
        )

    def build_proof(self, decision, frame):
        if decision.action == "retask":
            return self.evidence.build_proof(decision, frame)
        # Counterfactual non-retask envelopes are fixture inputs, not measured flight evidence.
        action = decision.action
        phase = frame.vtol_state
        required = {"continue": 12, "return": 12, "land-now": 4, "loiter": 6}.get(action)
        near_pad = math.hypot(*frame.position_ne_amsl[:2]) <= 12
        near_terrain = frame.terrain_conflict
        clear = required is not None and not (action == "continue" and near_terrain)
        return MotionProof(
            frame.frame_id,
            decision.digest,
            clear,
            5,
            5,
            required,
            frame.remaining_energy_wh,
            15 if phase == "fw" else None,
            17 if phase == "fw" else None,
            20 if phase == "fw" else 0,
            1,
            1,
            1,
            10,
            60,
            frame.remaining_time_s,
            landing_site_clear=action != "land-now" or near_pad,
            min_transition_agl_m=frame.agl_m if phase in {"fw", "front_transition"} else None,
            basis="ASSUMED_SIM",
        )


@dataclass
class Window:
    replay: Replay
    history: tuple
    frame: RuntimeFrame
    reading: InjectedTargetReading | None
    turns: list
    ended_ms: int

    def apply(self, raw, *, request, injected, now_ms):
        commands = []
        route = AirborneRouteSlot()
        executor = self.replay.executor(route, commands)
        for args in self.history:
            base = executor.open_request(**args)
        if any(request[key] != value for key, value in base.items()):
            raise ValueError("reconstructed request differs from the captured request")
        frame = replace(self.frame, t_ms=now_ms, event_id=base["event_id"])
        current = self.reading
        if (
            current is not None
            and frame.retask_boundary
            and now_ms - current.captured_ms <= 1000
            and any(
                row["tool"] == "target_status"
                and row["source"] == "fault"
                and row["data"] == current.data("target_status")
                for row in injected
            )
        ):
            frame = replace(
                frame,
                retask_finding_ne=x1_place_from_wgs84(
                    self.replay.terrain, *current.position_wgs84_amsl[:2]
                ),
                retask_finding_error_m=current.error_bound_m,
            )
        if frame.retask_boundary:
            point = frame.position_ne_amsl
            self.replay.evidence.check_entry(
                2, point, dict(x=point[0], y=point[1], z=-85, vx=16, vy=0, vz=0)
            )
            route.enter_tick(2)
        self.replay.evidence.stage_retask = route.stage
        admission = executor.apply(raw, frame, now_ms=now_ms, build_proof=self.replay.build_proof)
        installed = tuple(
            item.step.primitive for item in (route.at(i) for i in range(2, route.end or 2))
        )
        return admission, installed, len(commands)

    def replay_final(self, request, raw):
        return self.apply(
            raw,
            request=request,
            injected=[r for r in request["perception"]["results"] if r["ok"]],
            now_ms=self.ended_ms,
        )


def _make_window(replay, state, history, receipts, stage, case, variant, rng, t):
    index, phase, primitive, mode = PHASES[stage]
    # The cruise splice is at the serial FW passage boundary, not during a leg.
    at_boundary = case in {"target_found", "target_moved"}
    position = {
        "climb": (0, 0, 164 + variant),
        "transition": (0, 0, 185),
        "cruise": (320 if at_boundary else 210 + variant, 0, 185),
        "approach": (8 if case == "battery_land" else 70 + variant, 0, 138),
    }[stage]
    time_left = 940 - t // 1000
    energy = {"climb": 71, "transition": 70, "cruise": 67, "approach": 56}[stage] - rng.randrange(2)
    energy = {
        "energy_return": 32 + variant % 4,
        "battery_land": 10 + variant % 4,
        "battery_unavailable": None,
    }.get(case, energy)
    pct = (
        replay.scenario["battery_drop"]["pct"]
        if case in {"battery_land", "battery_unavailable"}
        else round(energy / 72 * 100)
    )
    distance = math.hypot(*position[:2])
    wind = (
        replay.scenario["wind_step"]
        if case == "wind_hold"
        else {"north_mps": 2, "east_mps": 1, "down_mps": 0}
    )
    link_down = case == "link_return"
    holds = {}
    if case in {"wind_hold", "wind_mild"}:
        holds["hold:survey-orbit"] = HoldSite(
            "fw",
            replay.terrain.origin_latitude_deg,
            replay.terrain.origin_longitude_deg,
            185,
            80,
            15,
        )
    reading = None
    events = {
        "target_found_event": "target_found",
        "stale_target": "target_found",
        "target_moved_event": "target_reacquired",
        "target_on_axis": "target_found",
        "target_lost": "target_lost",
        "tool_budget": "target_found",
        "energy_return": "battery_drop",
        "battery_land": "battery_drop",
        "battery_unavailable": "battery_drop",
        "wind_hold": "wind_step",
        "wind_mild": "wind_step",
        "link_return": "link_loss",
        "transition_no_override": "transition_problem",
        "px4_owns": "health_degraded",
    }
    track = None
    if case in {
        "target_found",
        "target_moved",
        "target_found_event",
        "target_moved_event",
        "target_on_axis",
        "stale_target",
        "tool_budget",
    }:
        marker = replay.scenario["target_found"]
        north = 405 + variant
        east = 95 + (variant % 5) * 3 + (14 if case.startswith("target_moved") else 0)
        if case == "target_on_axis":
            east = 0
        lat, lon = wgs84_forward_from_ned(
            latitude_deg=replay.terrain.origin_latitude_deg,
            longitude_deg=replay.terrain.origin_longitude_deg,
            north_offset_m=north,
            east_offset_m=east,
        )
        track = marker["track_ref"]
        reading = InjectedTargetReading(
            t - (1050 if case == "stale_target" else 30),
            track,
            marker["class"],
            round(math.degrees(math.atan2(east, north - position[0])), 3),
            marker["elevation_deg"],
            (lat, lon, 100),
            marker["position_estimate"]["error_bound_m"],
        )
        state.observe(
            t,
            "target",
            {
                "visible": True,
                "track_state": "tracking",
                "track_ref": track,
                "identity_confidence": 0.94,
            },
            source="fault",
        )
    if case == "target_lost":
        state.observe(
            t,
            "target",
            {"visible": False, "track_state": "lost", "track_ref": "track:1"},
            source="tracker",
        )
    telemetry = {
        "fc": {"armed": True, "system_status": 6 if case == "px4_owns" else 4},
        "battery": {"pct": pct, "charge_state": 3 if pct <= 25 else 1},
        "gps": {"fix_type": 3, "satellites": 16},
        "agl": {"m": position[2] - 100},
        "vtol": {"state": mode, "landed_state": 2},
        "air": {"airspeed_mps": 16 if mode == "fw" else None},
        "wind": {key: wind[key] for key in ("north_mps", "east_mps", "down_mps")},
        "link": {
            "gcs": "down" if link_down else "up",
            "heartbeat_age_ms": 6500 if link_down else 100,
        },
        "navigation": {
            "remaining_energy_wh": energy,
            "remaining_time_s": time_left,
            "home_distance_m": distance,
        },
    }
    if case == "battery_land":
        telemetry["terrain"] = {"clearance_m": 38, "conflict": False}
    sources = {
        "navigation": "executor",
        "link": "link_monitor",
        "wind": "fault" if case == "wind_hold" else "mavlink",
    }
    for group, values in telemetry.items():
        state.observe(t, group, values, source=sources.get(group, "mavlink"))
    step_id = replay.steps[index].source_step_id
    state.observe(
        t, "leg", {"step_id": step_id, "progress": 1 if at_boundary else 0.5}, source="executor"
    )
    if case in events:
        state.event(t, events[case], source="fault", severity="warning", confidence=0.95)
    plan_sha = canonical_sha256(
        {
            "compiled_plan_sha256": replay.plan_hash,
            "steps": [asdict(step) for step in replay.steps[index:]],
        }
    )
    progress = {
        "step_index": index,
        "step_id": step_id,
        "primitive": primitive,
        "phase": phase,
        "remaining_plan_sha256": plan_sha,
        "next_waypoint_distance_m": abs(320 - position[0]) if mode == "fw" else distance,
        "home_distance_m": distance,
        "eta_s": round(distance / 8, 1) if stage == "approach" else None,
        "remaining_energy_wh": energy,
        "remaining_time_s": time_left,
        "recent_receipts": list(receipts),
        "hold": {
            "active": False,
            "ref": None,
            "remaining_s": None,
            "native_action": None,
            "native_status": None,
        },
    }
    snapshot = state.snapshot(t, mission_id=replay.run_id, revision=1)
    # The target event itself cannot replace the route. An admitted splice uses
    # the following cadence request with a fresh result on that same ticket.
    opener = dict(
        snapshot=snapshot,
        run_id=replay.run_id,
        hold_refs=tuple(holds),
        remaining_plan_sha256=replay.plan_hash if case in events else plan_sha,
        mission_progress=None if case in events else progress,
    )
    # The unavailable battery tool does not remove the trusted executor-side
    # budget bound used to validate the existing suffix.
    frame = RuntimeFrame(
        t,
        t,
        replay.run_id,
        1,
        0,
        case == "px4_owns",
        True,
        mode,
        position[2] - 100,
        16 if mode == "fw" else None,
        True,
        holds,
        position_ne_amsl=position,
        active_motion_index=index,
        terrain_conflict=case == "wind_hold",
        remaining_energy_wh=energy if energy is not None else 54,
        remaining_time_s=time_left,
        retask_boundary=at_boundary,
    )
    return opener, Window(replay, (*history, opener), frame, reading, [], t)


def _run_window(window, budget):
    args = window.history[-1]
    executor = window.replay.executor(AirborneRouteSlot(), [])
    for options in window.history:
        request = executor.open_request(**options)
    t = args["snapshot"]["t_ms"]
    frame, reading = window.frame, window.reading
    north, east, alt = frame.position_ne_amsl
    bounded_energy = request["state"]["telemetry"]["navigation"]["remaining_energy_wh"]
    percent = request["state"]["telemetry"]["battery"]["pct"]
    route = RouteReading(
        t, window.replay.steps, frame.active_motion_index, (north, east, 100 - alt), (0, 0, 100), 30
    )
    sources = PerceptionSources(
        route=lambda: route,
        dem=window.replay.terrain,
        profile=window.replay.profile,
        battery=lambda: BatteryReading(
            percent, t, None if bounded_energy is None else bounded_energy + 1, 1, t
        ),
        injected_target=lambda: reading,
    )
    now = t

    def backend(sent):
        nonlocal now
        reply = policy(sent)
        window.turns.append((sent, reply))
        now += 25
        return canonical_json(reply)

    runner = ToolRunner(sources, max_calls=budget, clock_ms=lambda: now)
    reply = runner.run_window(request, backend, request["deadline_ms"])
    window.ended_ms = now
    reply.check_fresh(now)
    return request, window.apply(
        reply.wire, request=window.turns[-1][0], injected=reply.injected_results, now_ms=now
    )


def generate(split, root=ROOT, *, replicas=None):
    """Yield complete per-variant tickets and replay context; split before flattening turns."""
    seed, count = SPLITS[split]
    rng = random.Random(seed)
    for family, focus in FOCUS.items():
        for i in range(count if replicas is None else replicas):
            number = rng.randrange(10000, 99999)
            run_id = f"fl-{seed}-{list(FOCUS).index(family):02d}-{i:02d}"
            task = TASKS[split][i % 2].format(n=number)
            origin = {"train": (42.0, -93.7), "val": (46.2, -96.1), "holdout": (48.8, -100.1)}[
                split
            ]
            replay = Replay(root, run_id, task, origin)
            state, history, receipts = X1StateBuilder(), [], deque(maxlen=4)
            stages = [(p, f"quiet_{p}", "nominal") for p in PHASES]
            if focus is not None:
                j = list(PHASES).index(focus)
                stages = stages[: j + 1] + [(focus, family, family)]
                if family in {"target_found", "target_moved"}:
                    stages.insert(-1, (focus, "target_event", f"{family}_event"))
                if family == "target_moved":
                    stages[-2:-2] = [
                        (focus, "target_on_axis", "target_on_axis"),
                        (focus, "target_lost", "target_lost"),
                    ]
            previous_t = 0
            for tick, (stage, category, case) in enumerate(stages):
                t = previous_t + (650 if case in {"target_found", "target_moved"} else 9500)
                if not tick:
                    t = 2000 + i * 17
                previous_t = t
                opener, window = _make_window(
                    replay, state, history, receipts, stage, case, i, rng, t
                )
                request, (admission, installed, commands) = _run_window(
                    window, 1 if case == "tool_budget" else 3
                )
                history.append(opener)
                receipt = {
                    "request_id": request["request_id"],
                    "event_id": request["event_id"],
                    "trigger": request.get("trigger", "event"),
                    **asdict(admission),
                }
                receipts.append(receipt)
                if admission.status not in ADMITTED and commands:
                    raise ValueError(f"rejected decision recorded a command: {receipt}")
                yield category, window, receipt, installed


def rows_for_window(split, category, window):
    for request, label in window.turns:
        index = request["perception"]["call_index"]
        prompt = [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": canonical_json({"task": window.replay.task, "request": request}),
            },
        ]
        yield (
            {
                "sample_id": f"{request['request_id']}:{index}",
                "episode_id": window.replay.run_id,
                "split": split,
                "category": category,
                "source": "synthetic_cpu",
                "messages": prompt,
            },
            label,
        )


def build(output, root=ROOT, *, sample_size=200):
    output.mkdir(parents=True, exist_ok=True)
    dropped, sample_pool, stats = [], [], {}
    names = {"train": "train.jsonl", "val": "val.jsonl", "holdout": "x1-flight-loop-holdout.jsonl"}
    for split, name in names.items():
        rows, episodes, actions = [], set(), Counter()
        for category, window, receipt, _ in generate(split, root):
            episodes.add(window.replay.run_id)
            if receipt["status"] not in ADMITTED:
                dropped.append(
                    {
                        "split": split,
                        "episode_id": window.replay.run_id,
                        "category": category,
                        **receipt,
                    }
                )
                continue
            actions[receipt["action"]] += 1
            for row, label in rows_for_window(split, category, window):
                if split != "holdout":
                    row["messages"].append({"role": "assistant", "content": canonical_json(label)})
                rows.append(row)
        if split == "train":
            sample_pool = rows
        jsonl(output / name, rows)
        stats[split] = {
            "episodes": len(episodes),
            "tickets": sum(actions.values()),
            "turns": len(rows),
            "by_action": dict(actions),
            "by_category": dict(Counter(row["category"] for row in rows)),
        }
    # Sample whole episodes. Deterministic subset-sum keeps the review at exactly
    # the requested size without splitting a ticket or selecting a partial episode.
    episodes = {}
    for row in sample_pool:
        episodes.setdefault(row["episode_id"], []).append(row)
    chosen, remaining = [], sample_size
    groups = list(episodes.values())
    if sample_size:
        # Cover every available category first, including the last scenario families.
        uncovered = {row["category"] for row in sample_pool}
        for group in groups:
            categories = {row["category"] for row in group}
            if categories & uncovered and remaining >= len(group):
                chosen.append(group)
                remaining -= len(group)
                uncovered -= categories
        subsets = {0: []}
        used = {group[0]["episode_id"] for group in chosen}
        for group in groups:
            if group[0]["episode_id"] in used:
                continue
            for size, subset in list(subsets.items()):
                if size + len(group) <= remaining:
                    subsets.setdefault(size + len(group), [*subset, group])
            if remaining in subsets:
                break
        if remaining not in subsets:
            raise ValueError("sample size cannot be filled with complete episodes")
        chosen.extend(subsets[remaining])
    jsonl(output / "sample.jsonl", (row for group in chosen for row in group))
    jsonl(output / "rejected-tickets.jsonl", dropped)
    artifacts = [*names.values(), "sample.jsonl", "rejected-tickets.jsonl"]
    manifest = {
        "schema": SCHEMA,
        "split": stats,
        "split_seed_and_replicas": SPLITS,
        "rejected_by_reason": dict(Counter(row["reason"] for row in dropped)),
        "sample_turns": sample_size,
        "sha256": {
            name: hashlib.sha256((output / name).read_bytes()).hexdigest() for name in artifacts
        },
    }
    (output / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--reference-root", type=Path, default=ROOT, help="config root for old-base lanes"
    )
    args = parser.parse_args(argv)
    print(canonical_json(build(args.output, args.reference_root.resolve())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
