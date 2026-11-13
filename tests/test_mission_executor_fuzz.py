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

ROOT = Path(__file__).resolve().parents[1]
# Debug trace of the last fuzz case; the old Windows scratch path stays the default there.
SCRATCH = Path(
    os.environ.get("MULTIDETECT_FUZZ_TRACE_DIR")
    or ("D:/Projects/mdwt/_scratch/cx-executor-fuzz" if os.name == "nt" else ROOT / ".tmp/cx-executor-fuzz")
)
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
    package_backed: bool


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
    if skill == "follow":
        # Keep one deterministic live-target route aligned with yaw=0 so a
        # boundary mutation must produce a translate rather than only turns.
        pose[0] = -half_n * 0.5
        pose[1] = home[1]
        # Keep the boundary-focused follow route independent of obstacle
        # detours; other generated skills cover no-fly geometry.
        zones.clear()
    duration = 100.0 if skill == "follow" else rng.uniform(1.0, 4.0)
    return Scenario(
        seed,
        area,
        home,
        pose,
        tuple(zones),
        skill,
        duration,
        force_failure,
        seed % 2 == 0,
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


def _executor(
    s: Scenario,
    *,
    package_backed: bool | None = None,
    runtime_step_limit: int | None = None,
    step_budget: int | None = None,
) -> MissionExecutor:
    if package_backed is None:
        package_backed = s.package_backed
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
    no_fly_polygons = tuple(
        tuple(
            (
                centre[0] + (radius + 3.0) * math.cos(2.0 * math.pi * index / 32.0),
                centre[1] + (radius + 3.0) * math.sin(2.0 * math.pi * index / 32.0),
            )
            for index in range(32)
        )
        for centre, radius in s.zones
    )
    adapter.safety = SafetyEnvelope(
        SafetyEnvelopeConfig(
            operating_polygon=polygon,
            no_fly_polygons=no_fly_polygons,
            min_altitude_m=s.area["min_altitude_m"],
            max_altitude_m=s.area["max_altitude_m"],
            vehicle=adapter.safety.vehicle,
        ),
        # Deliberately seed a different home so the package-binding mutation is
        # observable; non-package initialization overwrites this on first pose.
        home_ned=(0.0, 0.0, 0.0),
    )
    package = (
        SimpleNamespace(
            document={"home": list(s.home), "operating_area": s.area}, no_fly_zones=s.zones
        )
        if package_backed
        else None
    )
    selected_step_budget = (1 if s.force_failure else 18) if step_budget is None else step_budget
    return MissionExecutor(
        _plan(s),
        loop_factory=make,
        refs={
            "zone:r": (0.0, 0.0, 25.0),
            "route:r": [(s.home[0], s.home[1]), (s.pose[0], s.pose[1])],
        },
        config=ExecutorConfig(
            step_budget=selected_step_budget,
            travel_budget=24,
            tracking_budget_max=24,
            count_budget_max=24,
            max_translate_m=8,
            runtime_step_limit=runtime_step_limit,
            runtime_tail_reserve=3,
        ),
        mission_package=package,
        no_fly_zones=s.zones,
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


def _run(seed: int, *, package_backed: bool | None = None, runtime_step_limit: int | None = None) -> None:
    s = _scenario(seed)
    ex = _executor(s, package_backed=package_backed, runtime_step_limit=runtime_step_limit)
    actual_package_backed = s.package_backed if package_backed is None else package_backed
    initial_pose = tuple(s.pose)
    if actual_package_backed:
        assert tuple(ex.airframe_adapter.safety.home_ned) == (*s.home, 0.0)
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
        + 182
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
            if s.skill == "follow":
                # Some tracks are deliberately outside the package area.  A
                # valid executor must refuse the leg while keeping its pose
                # inside the envelope; this is the randomized boundary case.
                target = (s.area["north_max_m"] + 20.0, s.home[1])
            else:
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
                    track.track_id, "approach_track" if tick_rng.random() > 0.2 else "reject_track"
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
        if not actual_package_backed and tick == 0:
            assert tuple(ex.airframe_adapter.safety.home_ned) == (
                initial_pose[0],
                initial_pose[1],
                0.0,
            ), "non-package airborne home must retain ground-level envelope reference"
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
    altitude_rejected = sum(
        "min_altitude" in row["info"].get("safety", {}).get("reasons", [])
        for row in trace
    )
    if not s.force_failure and not s.zones and s.skill in {"patrol", "monitor"}:
        assert rejected / max(1, len(trace)) <= 0.5, f"seed {seed}: excessive safety refusals"
    if not actual_package_backed and not s.force_failure:
        assert altitude_rejected / max(1, len(trace)) <= 0.2, (
            f"seed {seed}: non-package airborne home caused systematic min_altitude refusal"
        )
    SCRATCH.mkdir(parents=True, exist_ok=True)
    (SCRATCH / "last_fuzz_trace.json").write_text(
        json.dumps(
            {"seed": seed, "scenario": s.__dict__, "history": ex.history, "trace": trace},
            default=str,
            indent=2,
        ),
        encoding="utf-8",
    )


def _run_campaign(seeds: list[int]) -> tuple[int, int]:
    package_count = 0
    non_package_count = 0
    for seed in seeds:
        _run(seed)
        if _scenario(seed).package_backed:
            package_count += 1
        else:
            non_package_count += 1
    return package_count, non_package_count


@pytest.mark.onboard
def test_mission_executor_random_invariants():
    seeds = _seeds()
    package_count, non_package_count = _run_campaign(seeds)
    assert package_count >= math.ceil(len(seeds) * 0.3)
    assert non_package_count >= math.ceil(len(seeds) * 0.3)


def _normal_cruise_trace(ex: MissionExecutor, s: Scenario, limit: int = 30):
    pose = list(s.pose)
    yaw = 0.0
    trace = []
    for tick in range(limit):
        action, info = ex.next_action(
            pose_ned=tuple(pose), yaw_deg=yaw, timestamp_s=float(tick), frame_id=f"mutation-{tick}"
        )
        trace.append((action, info, tuple(pose)))
        yaw = _apply(pose, yaw, action)
        if ex.done:
            break
    return pose, trace


@pytest.mark.onboard
def test_mutation_home_altitude_is_caught(monkeypatch):
    """69fa6714 mutation must fail the same randomized campaign."""
    original = MissionExecutor._init_home

    def airborne_home(self, pose):
        original(self, pose)
        if not self._package_backed:
            self.airframe_adapter.safety.set_home((pose[0], pose[1], pose[2]))

    monkeypatch.setattr(MissionExecutor, "_init_home", airborne_home)
    with pytest.raises(AssertionError, match="airborne home|safety refusal"):
        _run_campaign(list(range(40)))


@pytest.mark.mutation_full
def test_mutation_package_home_binding_is_caught(monkeypatch):
    s = _scenario(2)
    original = MissionExecutor.__init__

    def unbound(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.airframe_adapter.safety.home_ned = (self.home[0], self.home[1], -30.0)

    monkeypatch.setattr(MissionExecutor, "__init__", unbound)
    with pytest.raises(AssertionError, match="home_ned"):
        ex = _executor(s)
        assert tuple(ex.airframe_adapter.safety.home_ned) == (*s.home, 0.0), "home_ned package binding lost"


@pytest.mark.mutation_full
def test_mutation_runtime_count_budget_is_caught(monkeypatch):
    s = _scenario(3)
    s.skill = "count"
    s.force_failure = False
    ex = _executor(s, runtime_step_limit=12, step_budget=140)
    def unbounded_count(self, step, run):
        return self.config.count_budget_max

    monkeypatch.setattr(MissionExecutor, "_count_budget", unbounded_count)
    pose, trace = _normal_cruise_trace(ex, s, limit=20)
    with pytest.raises(AssertionError, match="runtime step limit"):
        assert len(trace) <= ex.config.runtime_step_limit, "count mutation exceeded runtime step limit"


@pytest.mark.onboard
def test_mutation_follow_boundary_is_caught(monkeypatch):
    original_guard = AirframeMotionAdapter.guard_command

    def unsafe_follow(self, run, pose, yaw, ground_point, frame_id):
        return ActionPrimitive.translate(100.0, 0.0, 0.0), {"mutation": "follow_boundary_removed"}

    def no_follow_boundary(self, command, **kwargs):
        if kwargs.get("skill") == "follow":
            return command, {}
        return original_guard(self, command, **kwargs)

    monkeypatch.setattr(MissionExecutor, "_follow", unsafe_follow)
    monkeypatch.setattr(AirframeMotionAdapter, "guard_command", no_follow_boundary)
    with pytest.raises(AssertionError, match="outside area|systematic min_altitude|ground-level"):
        _run_campaign(list(range(40)))


@pytest.mark.mutation_full
def test_mutation_no_fly_segment_is_caught(monkeypatch):
    s = _scenario(5)
    s.skill = "area_search"
    s.zones = (((0.0, 0.0), 3.0),)
    s.pose = [-8.0, 0.0, -20.0]
    ex = _executor(s)
    monkeypatch.setattr(ex, "_translate_enters_no_fly", lambda *_args: False)
    monkeypatch.setattr(ex.airframe_adapter, "guard_command", lambda command, **_kwargs: (command, {}))
    pose = list(s.pose)
    action, _ = ex.next_action(pose_ned=tuple(pose), yaw_deg=0.0)
    _apply(pose, 0.0, action)
    with pytest.raises(AssertionError, match="no-fly segment"):
        assert math.dist((pose[0], pose[1]), (0.0, 0.0)) >= 6.0, "no-fly segment mutation entered the zone"


@pytest.mark.mutation_full
def test_mutation_failure_link_is_caught(monkeypatch):
    s = _scenario(6)
    s.skill = "area_search"
    s.force_failure = True
    ex = _executor(s)
    ex.active_loop.state = "blocked"
    original = MissionExecutor._finish

    def no_failure_link(self, state, reason=None):
        if state == "failed":
            self.history.append({"step": self.current_step.id, "skill": self.current_step.skill, "state": state, "reason": reason})
            self.done = True
            self._run = None
            return
        return original(self, state, reason)

    monkeypatch.setattr(MissionExecutor, "_finish", no_failure_link)
    _pose, trace = _normal_cruise_trace(ex, s)
    with pytest.raises(AssertionError, match="return_home"):
        assert any(row["skill"] == "return_home" for row in ex.history), "failure path did not link to return_home"
