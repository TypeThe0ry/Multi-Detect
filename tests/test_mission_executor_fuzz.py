"""Deterministic property checks for the onboard mission executor.

The default set is deliberately small enough for the onboard gate.  The same
runner accepts ``MISSION_FUZZ_SEEDS=300`` (or ``all``) for the campaign.
"""
# ruff: noqa: E501

from __future__ import annotations

import json
import math
import os
import random
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.autonomy.mission_executor import ExecutorConfig, MissionExecutor
from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.motion_intent import AirframeMotionAdapter
from multidetect.autonomy.onboard_loop import CameraModel, Detection, LoopConfig, OnboardLoop
from multidetect.autonomy.safety_envelope import SafetyEnvelope, SafetyEnvelopeConfig
from multidetect.sim_teacher.primitives import ActionPrimitive

pytestmark = pytest.mark.onboard

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = Path("D:/Projects/mdwt/_scratch/cx-executor-fuzz")
CAMERA = CameraModel(fx=540, fy=540, cx=640, cy=480, width=1280, height=960)


@dataclass
class Scenario:
    seed: int
    area: dict[str, float]
    home: tuple[float, float]
    pose: list[float]
    zones: tuple[tuple[tuple[float, float], float], ...]
    skill: str
    duration: float
    force_failure: bool


def _seeds() -> list[int]:
    value = os.getenv("MISSION_FUZZ_SEEDS", "40").strip().lower()
    if value in {"all", "full"}:
        return list(range(300))
    count = max(1, int(value))
    return list(range(count))


def _scenario(seed: int) -> Scenario:
    rng = random.Random(seed)
    half_n = rng.uniform(35.0, 70.0)
    half_e = rng.uniform(35.0, 70.0)
    area = {
        "north_min_m": -half_n,
        "north_max_m": half_n,
        "east_min_m": -half_e,
        "east_max_m": half_e,
        "min_altitude_m": rng.uniform(6.0, 10.0),
        "max_altitude_m": rng.uniform(35.0, 70.0),
    }
    home = (rng.uniform(-half_n * 0.35, half_n * 0.35), rng.uniform(-half_e * 0.35, half_e * 0.35))
    pose = [
        rng.uniform(-half_n * 0.8, half_n * 0.8),
        rng.uniform(-half_e * 0.8, half_e * 0.8),
        -rng.uniform(area["min_altitude_m"], area["max_altitude_m"] * 0.7),
    ]
    zones = []
    for _ in range(rng.randint(0, 3)):
        centre = (
            rng.uniform(-half_n * 0.6, half_n * 0.6),
            rng.uniform(-half_e * 0.6, half_e * 0.6),
        )
        radius = rng.uniform(1.0, 3.0)
        if math.dist(centre, home) > radius + 8 and math.dist(centre, pose[:2]) > radius + 8:
            zones.append((centre, radius))
    skill = rng.choice(("area_search", "count", "patrol", "monitor", "follow"))
    force_failure = seed % 5 == 0
    if force_failure:
        skill = "area_search"
    return Scenario(
        seed, area, home, pose, tuple(zones), skill, rng.uniform(1.0, 4.0), force_failure
    )


def _plan(s: Scenario):
    region = "zone:r"
    if s.skill == "patrol":
        step = {
            "id": "work",
            "skill": "patrol",
            "params": {
                "route_ref": "route:r",
                "query": "car",
                "loops": 1,
                "duration_s": s.duration,
            },
            "on_success": "home",
            "on_fail": "home",
        }
    elif s.skill == "monitor":
        step = {
            "id": "work",
            "skill": "monitor",
            "params": {"region": region, "query": "car", "duration_s": s.duration},
            "on_success": "home",
            "on_fail": "home",
        }
    elif s.skill == "follow":
        step = {
            "id": "work",
            "skill": "follow",
            "params": {"track_ref": "track:1", "query": "car", "duration_s": s.duration},
            "on_success": "home",
            "on_fail": "home",
        }
    else:
        step = {
            "id": "work",
            "skill": s.skill,
            "params": {"region": region, "query": "car"},
            "on_success": "home",
            "on_fail": "home",
        }
    return validate_mission_plan(
        {
            "plan_version": 1,
            "goal": "fuzz",
            "steps": [
                step,
                {
                    "id": "home",
                    "skill": "return_home",
                    "params": {},
                    "on_success": None,
                    "on_fail": None,
                },
            ],
            "report": ["findings", "positions"],
        }
    )


def _executor(s: Scenario) -> MissionExecutor:
    def make(_text, region=None, **overrides):
        overrides.pop("static_ground_veto", None)
        overrides.pop("stop_on_confirm", None)
        overrides.pop("max_steps", None)
        return OnboardLoop(
            camera=CAMERA,
            search_action=lambda pose, yaw: ActionPrimitive.translate(8, 0, 0),
            config=LoopConfig(**overrides),
        )

    adapter = AirframeMotionAdapter("x500_mono_cam", base_dir=ROOT)
    polygon = (
        (s.area["north_min_m"], s.area["east_min_m"]),
        (s.area["north_max_m"], s.area["east_min_m"]),
        (s.area["north_max_m"], s.area["east_max_m"]),
        (s.area["north_min_m"], s.area["east_max_m"]),
    )
    adapter.safety = SafetyEnvelope(
        SafetyEnvelopeConfig(
            operating_polygon=polygon,
            min_altitude_m=s.area["min_altitude_m"],
            max_altitude_m=s.area["max_altitude_m"],
            vehicle=adapter.safety.vehicle,
        ),
        home_ned=(*s.home, 0.0),
    )
    package = SimpleNamespace(
        document={"home": list(s.home), "operating_area": s.area}, no_fly_zones=s.zones
    )
    return MissionExecutor(
        _plan(s),
        loop_factory=make,
        refs={
            "zone:r": (0.0, 0.0, 25.0),
            "route:r": [(s.home[0], s.home[1]), (s.pose[0], s.pose[1])],
        },
        config=ExecutorConfig(
            step_budget=1 if s.force_failure else 18,
            travel_budget=24,
            tracking_budget_max=24,
            count_budget_max=24,
            max_translate_m=8,
        ),
        mission_package=package,
        airframe_adapter=adapter,
    )


def _apply(pose: list[float], yaw: float, action: ActionPrimitive) -> float:
    if action.kind == "turn":
        return (yaw + action.yaw_delta_deg + 180.0) % 360.0 - 180.0
    if action.kind == "translate":
        heading = math.radians(yaw)
        pose[0] += action.forward_m * math.cos(heading) - action.right_m * math.sin(heading)
        pose[1] += action.forward_m * math.sin(heading) + action.right_m * math.cos(heading)
        pose[2] -= action.up_m
    return yaw


def _inside(p: tuple[float, float], area: dict[str, float]) -> bool:
    return (
        area["north_min_m"] - 1e-6 <= p[0] <= area["north_max_m"] + 1e-6
        and area["east_min_m"] - 1e-6 <= p[1] <= area["east_max_m"] + 1e-6
    )


def _run(seed: int) -> None:
    s = _scenario(seed)
    ex = _executor(s)
    assert tuple(ex.airframe_adapter.safety.home_ned[:2]) == s.home
    pose = list(s.pose)
    yaw = 0.0
    trace = []
    config = ex.config
    step_bound = (
        max(
            config.step_budget,
            config.travel_budget,
            config.tracking_budget_max,
            config.count_budget_max,
        )
        + config.travel_budget
        + config.landing_timeout_steps
        + len(ex.plan.steps) * 2
    )
    for tick in range(step_bound):
        tick_rng = random.Random((seed + 1) * 1009 + tick)
        detections = ()
        ground = None
        # The target deliberately changes visibility, position and identity
        # verdict.  This exercises stale-track and re-identification paths
        # without making the test depend on wall clock or a model service.
        if not s.force_failure and tick_rng.random() > 0.25:
            target = (
                s.home[0] + 4.0 + math.sin(tick / 3.0),
                s.home[1] + 4.0 + math.cos(tick / 4.0),
            )
            detections = (Detection((620, 680, 660, 720), 0.9),)

            def ground(_u, _v, target=target):
                return target
        tracks = ex.observe(detections, pose_ned=tuple(pose), yaw_deg=yaw, ground_point=ground)
        for track in tracks:
            if s.skill == "follow":
                ex.apply_verdict(
                    track.track_id, "follow_track" if tick_rng.random() > 0.2 else "reject_track"
                )
            else:
                ex.apply_verdict(
                    track.track_id, "confirm_target" if tick_rng.random() > 0.35 else "reject_track"
                )
        before = tuple(pose)
        action, info = ex.next_action(
            pose_ned=tuple(pose),
            yaw_deg=yaw,
            ground_point=ground,
            frame_id=f"f{tick}",
            timestamp_s=float(tick),
        )
        yaw = _apply(pose, yaw, action)
        trace.append({"tick": tick, "action": action.kind, "pose": list(pose), "info": info})
        assert _inside((pose[0], pose[1]), s.area), (
            f"seed {seed}: outside area at {tick} {trace[-1]}"
        )
        altitude = -pose[2]
        assert s.area["min_altitude_m"] - 1e-6 <= altitude <= s.area["max_altitude_m"] + 1e-6, (
            f"seed {seed}: altitude violation"
        )
        for centre, radius in s.zones:
            assert math.dist((pose[0], pose[1]), centre) >= radius + 3.0 - 1e-5, (
                f"seed {seed}: no-fly violation"
            )
            dn, de = pose[0] - before[0], pose[1] - before[1]
            length2 = dn * dn + de * de
            t = (
                0.0
                if length2 == 0
                else max(
                    0.0,
                    min(
                        1.0, ((centre[0] - before[0]) * dn + (centre[1] - before[1]) * de) / length2
                    ),
                )
            )
            closest = (before[0] + t * dn, before[1] + t * de)
            assert math.dist(closest, centre) >= radius + 3.0 - 1e-5, (
                f"seed {seed}: no-fly segment violation"
            )
        if ex.done:
            break
    assert ex.done, (
        f"seed {seed}: non-terminating current={ex.current_step} trace_tail={trace[-5:]}"
    )
    assert any(row["skill"] == "return_home" for row in ex.history), (
        f"seed {seed}: no return-home path"
    )
    if s.force_failure:
        assert ex.history[0]["state"] == "failed" and ex.history[1]["skill"] == "return_home"
    report = ex.report()
    assert report.positions == tuple(
        f"{f['position_ne'][0]:.1f},{f['position_ne'][1]:.1f}" for f in ex.findings
    )
    assert report.findings == tuple(str(f["query"]) for f in ex.findings)
    assert report.count == (sum(ex.counts.values()) if ex.counts else None)
    rejected = sum(bool(row["info"].get("safety", {}).get("rejected")) for row in trace)
    if not s.force_failure and not s.zones and s.skill in {"patrol", "monitor"}:
        assert rejected / max(1, len(trace)) <= 0.5, f"seed {seed}: excessive safety refusals"
    SCRATCH.mkdir(parents=True, exist_ok=True)
    (SCRATCH / "last_fuzz_trace.json").write_text(
        json.dumps(
            {"seed": seed, "scenario": s.__dict__, "history": ex.history, "trace": trace},
            default=str,
            indent=2,
        ),
        encoding="utf-8",
    )


def test_mission_executor_random_invariants():
    for seed in _seeds():
        _run(seed)
