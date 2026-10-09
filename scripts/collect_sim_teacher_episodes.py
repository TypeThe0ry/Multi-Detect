#!/usr/bin/env python3
"""Collect raw SIM teacher episodes in disposable PX4/Gazebo containers.

Runtime recording deliberately does no provenance hashing.  Run the offline
auditor after collection.  ``--dry-run`` validates the scenario and prints the
planned launch without starting Docker.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.sim_teacher import (  # noqa: E402
    EpisodeRecorder,
    ExpertSearchPolicy,
    ExploreBounds,
    ExplorePolicy,
)
from multidetect.sim_teacher.isolation import validate_local_endpoint  # noqa: E402
from scripts.run_simulator_docker import build_docker_simulator_plan  # noqa: E402

PINNED_IMAGE = (
    "px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8"
)
DEFAULT_TIMEOUT_S = 300.0
DEFAULT_CAMERA_INFO = {
    "width": 640,
    "height": 480,
    "fx": 320.0,
    "fy": 320.0,
    "cx": 320.0,
    "cy": 240.0,
}


@dataclass(frozen=True, slots=True)
class CollectionPlan:
    scenario: str
    runtime: str
    truth: str
    teacher: str
    episodes: int
    steps_per_episode: int
    seed: int
    out: str
    image: str = PINNED_IMAGE
    container_prefix: str = "md-sim-teacher"
    timeout_s: float = DEFAULT_TIMEOUT_S

    def to_mapping(self) -> dict[str, Any]:
        result = asdict(self)
        result["docker_command"] = docker_command(self)
        return result


def _scenario_channels(scenario_path: Path) -> tuple[Path, Path]:
    document = json.loads(scenario_path.read_text(encoding="utf-8"))
    channels = document.get("channels")
    if not isinstance(channels, dict):
        raise ValueError("scenario must contain channels")

    def resolve(name: str) -> Path:
        channel = channels.get(name)
        if not isinstance(channel, dict) or not isinstance(channel.get("path"), str):
            raise ValueError(f"scenario channel {name!r} is missing path")
        path = (scenario_path.parent / channel["path"]).resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    return resolve("runtime_input"), resolve("truth")


def _validate_runtime(runtime_path: Path) -> dict[str, Any]:
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    if not isinstance(runtime, dict):
        raise ValueError("runtime channel must be a JSON object")
    vehicle = runtime.get("vehicle", {})
    model = vehicle.get("model") if isinstance(vehicle, dict) else None
    if model and "multirotor" not in str(model).lower() and "x500" not in str(model).lower():
        raise ValueError("SIM teacher collector currently supports multirotor only")
    for sensor in runtime.get("sensors", []):
        if isinstance(sensor, dict) and isinstance(sensor.get("stream_uri"), str):
            validate_local_endpoint(sensor["stream_uri"])
    return runtime


def docker_command(plan: CollectionPlan, *, name: str | None = None) -> list[str]:
    """Build the repository-owned isolated PX4/Gazebo command for one episode."""
    container_name = name or f"{plan.container_prefix}-preview"
    try:
        stack_plan = build_docker_simulator_plan(
            Path(plan.scenario),
            repository_root=ROOT,
            container_name=container_name,
            job_dir=Path(plan.out) / "jobs" / container_name,
            dry_run=True,
        )
    except Exception as exc:
        raise ValueError(f"unable to build simulator stack plan: {exc}") from exc
    return list(stack_plan.command)


def _docker_available() -> bool:
    executable = shutil.which("docker")
    if not executable:
        return False
    try:
        result = subprocess.run(
            [executable, "version"], capture_output=True, timeout=8, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


class DockerSimEnvironment:
    """Own one simulator container and always remove it on exit."""

    def __init__(self, plan: CollectionPlan, episode_id: str):
        self.plan = plan
        self.name = f"{plan.container_prefix}-{episode_id}"
        self.started = False

    def __enter__(self) -> DockerSimEnvironment:
        result = subprocess.run(
            docker_command(self.plan, name=self.name),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "Docker launch failed").strip()
            raise RuntimeError(f"Docker launch failed ({result.returncode}): {detail[:500]}")
        self.started = True
        return self

    def wait(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}", self.name],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode != 0 or result.stdout.strip().lower() != "true":
                return
            time.sleep(0.25)
        raise TimeoutError(f"SIM container exceeded wall-clock timeout {timeout_s:.1f}s")

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.started:
            subprocess.run(
                ["docker", "rm", "--force", self.name],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.started = False
        return False


def _truth_target(truth_path: Path) -> tuple[tuple[float, float, float], str]:
    data = json.loads(truth_path.read_text(encoding="utf-8"))
    targets = data.get("targets", []) if isinstance(data, dict) else []
    target = targets[0] if isinstance(targets, list) and targets else {}
    label = str(target.get("label", "target")) if isinstance(target, dict) else "target"
    position = target.get("position", {}) if isinstance(target, dict) else {}
    if not isinstance(position, dict):
        return (20.0, 0.0, 0.0), label
    if "north_m" in position or "east_m" in position:
        north = float(position.get("north_m", 20.0))
        east = float(position.get("east_m", 0.0))
        up = float(position.get("up_m", 0.0))
        return (north, east, -up), label
    # The checked-in urban world has this WGS-84 origin.  Until the live
    # GPS_GLOBAL_ORIGIN message is decoded, use its local tangent conversion.
    lat0, lon0 = 1.352100, 103.819800
    lat = float(position.get("latitude_deg", lat0))
    lon = float(position.get("longitude_deg", lon0))
    north = (lat - lat0) * 111_132.0
    east = (lon - lon0) * 111_320.0 * math.cos(math.radians(lat0))
    return (north, east, 0.0), label


def _policy_for(plan: CollectionPlan, truth_path: Path, seed: int):
    if plan.teacher == "explore":
        return ExplorePolicy(seed=seed, bounds=ExploreBounds())
    target, label = _truth_target(truth_path)
    return ExpertSearchPolicy(target_ned=target, intrinsics=DEFAULT_CAMERA_INFO), label


def _placeholder_observation(step_index: int) -> dict[str, Any]:
    return {
        "camera_info": dict(DEFAULT_CAMERA_INFO),
        "pose": {"local_ned_m": [0.0, 0.0, -5.0], "lat_deg": None, "lon_deg": None, "alt_m": 5.0},
        "attitude": {"roll_deg": 0.0, "pitch_deg": 0.0, "yaw_deg": 0.0},
        "velocity": [0.0, 0.0, 0.0],
        "gps_fix": {"fix_type": 0, "satellites": 0},
        "sensor_status": "awaiting_live_gazebo_stream",
        "step": step_index,
    }


def run_episode(plan: CollectionPlan, truth_path: Path, episode_index: int) -> Path:
    episode_id = f"episode-{episode_index:04d}-{uuid.uuid4().hex[:8]}"
    policy_result = _policy_for(plan, truth_path, plan.seed + episode_index)
    if plan.teacher == "expert_search":
        policy, target_label = policy_result
        task_text = f"找到{target_label}并记录位置"
    else:
        policy = policy_result
        task_text = "探索场景并记录观测"
    recorder = EpisodeRecorder(plan.out, episode_id, task_text=task_text)
    started = time.monotonic()
    try:
        with DockerSimEnvironment(plan, episode_id) as environment:
            recorder.record_mavlink(
                {
                    "message_type": "collector_container_started",
                    "container": environment.name,
                    "image": plan.image,
                }
            )
            for index in range(plan.steps_per_episode):
                if time.monotonic() - started > plan.timeout_s:
                    raise TimeoutError("episode wall-clock timeout")
                if plan.teacher == "expert_search":
                    action = policy.next_action(pose_ned=(0.0, 0.0, -5.0), yaw_deg=0.0)
                    labels = policy.labels(pose_ned=(0.0, 0.0, -5.0), yaw_deg=0.0)
                else:
                    action = policy.next_action()
                    labels = {}
                observation = _placeholder_observation(index)
                recorder.record_step(
                    observation,
                    action,
                    ack={"container": environment.name},
                    outcome={"status": "recorded"},
                    obs_after=observation,
                    labels=labels,
                )
            recorder.record_mavlink(
                {"message_type": "collector_episode_complete", "container": environment.name}
            )
    except Exception as exc:
        recorder.meta["failure"] = f"{type(exc).__name__}: {exc}"
        recorder.episode_path.write_text(json.dumps(recorder.meta, indent=2), encoding="utf-8")
        raise
    return recorder.episode_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--teacher", choices=("explore", "expert_search"), required=True)
    parser.add_argument("--episodes", type=int, required=True)
    parser.add_argument("--steps-per-episode", type=int, required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.episodes < 1 or args.steps_per_episode < 1:
        raise SystemExit("--episodes and --steps-per-episode must be positive")
    if not 1.0 <= args.timeout_s <= 86400.0:
        raise SystemExit("--timeout-s must be between 1 and 86400 seconds")
    scenario = args.scenario.resolve()
    if not scenario.is_file():
        raise SystemExit(f"scenario not found: {scenario}")
    runtime_path, truth_path = _scenario_channels(scenario)
    _validate_runtime(runtime_path)
    plan = CollectionPlan(
        str(scenario),
        str(runtime_path),
        str(truth_path),
        args.teacher,
        args.episodes,
        args.steps_per_episode,
        args.seed,
        str(args.out.resolve()),
        timeout_s=args.timeout_s,
    )
    if args.dry_run:
        print(json.dumps(plan.to_mapping(), ensure_ascii=False, indent=2))
        return 0
    if not _docker_available():
        raise SystemExit("Docker daemon is not reachable; use --dry-run or start Docker")
    args.out.mkdir(parents=True, exist_ok=True)
    completed: list[str] = []
    for index in range(args.episodes):
        path = run_episode(plan, truth_path, index)
        completed.append(str(path))
        print(json.dumps({"episode": index, "path": str(path)}, ensure_ascii=False))
    print(json.dumps({"episodes": completed, "out": str(args.out.resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
