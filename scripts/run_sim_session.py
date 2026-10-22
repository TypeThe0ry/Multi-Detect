#!/usr/bin/env python3
"""Start an interactive PX4/Gazebo session in Docker Desktop.

The session owns one container and removes it on Ctrl+C.  It deliberately
reuses ``run_simulator_docker`` for the pinned image and repository asset
contract, then adds the WSLg display bindings needed by Gazebo's client.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scripts.run_simulator_docker import (  # noqa: E402
    PINNED_IMAGE,
    DockerSimulatorPlan,
    build_docker_simulator_plan,
)

DEFAULT_WORLD = "urban_inspection_multirotor_001.sdf"
WORLD_SCENARIOS = {
    "urban_inspection_multirotor_001.sdf": ROOT
    / "configs/scenarios/urban_inspection_multirotor_001.scenario.json",
    "river_rescue_vtol_001.sdf": ROOT / "configs/scenarios/river_rescue_vtol_001.scenario.json",
    "coastal_patrol_fixed_wing_001.sdf": ROOT
    / "configs/scenarios/coastal_patrol_fixed_wing_001.scenario.json",
}
AIRFRAME_SCENARIOS = {
    "x500": WORLD_SCENARIOS["urban_inspection_multirotor_001.sdf"],
    "x500_mono_cam": WORLD_SCENARIOS["urban_inspection_multirotor_001.sdf"],
    "standard_vtol": WORLD_SCENARIOS["river_rescue_vtol_001.sdf"],
    "tiltrotor": WORLD_SCENARIOS["river_rescue_vtol_001.sdf"],
    "rc_cessna": WORLD_SCENARIOS["coastal_patrol_fixed_wing_001.sdf"],
    "rc_cessna_runway": WORLD_SCENARIOS["coastal_patrol_fixed_wing_001.sdf"],
    "rc_cessna_hand": WORLD_SCENARIOS["coastal_patrol_fixed_wing_001.sdf"],
    "rc_cessna_catapult": WORLD_SCENARIOS["coastal_patrol_fixed_wing_001.sdf"],
}


def resolve_world(value: str | Path) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        if candidate.suffix == "":
            candidate = candidate.with_suffix(".sdf")
        by_name = ROOT / "simulation/gazebo/worlds" / candidate
        candidate = by_name if by_name.is_file() else ROOT / candidate
    candidate = candidate.resolve()
    if not candidate.is_file() or candidate.suffix.lower() != ".sdf":
        raise ValueError(f"world not found or not an SDF file: {value}")
    if ROOT not in candidate.parents:
        raise ValueError("world must be inside the repository")
    return candidate


def _scenario_for(world: Path, airframe: str) -> Path:
    for name, scenario in WORLD_SCENARIOS.items():
        if world.name == name:
            return scenario
    try:
        return AIRFRAME_SCENARIOS[airframe]
    except KeyError as exc:
        raise ValueError(
            f"airframe {airframe!r} has no known scenario; use x500, standard_vtol, or rc_cessna"
        ) from exc


def _replace_world_mount(command: list[str], world: Path) -> None:
    for index, token in enumerate(command):
        if token == "--mount" and index + 1 < len(command):
            spec = command[index + 1]
            if "target=/opt/px4-gazebo/share/gz/worlds/default.sdf" in spec:
                command[index + 1] = re.sub(
                    r"(?:^|,)source=[^,]+",
                    lambda _: f",source={world}",
                    spec,
                    count=1,
                ).lstrip(",")
                return
    raise ValueError("base Docker plan did not contain its world mount")


def build_session_command(
    *,
    world: str | Path = DEFAULT_WORLD,
    airframe: str = "x500_mono_cam",
    gcs_port: int | None = None,
    viewer: str = "wslg",
    container_name: str | None = None,
    control_port: int = 14540,
    telemetry_port: int = 14541,
) -> tuple[list[str], DockerSimulatorPlan, Path]:
    """Build the Docker argv without starting Docker (unit-test boundary)."""
    world_path = resolve_world(world)
    scenario = _scenario_for(world_path, airframe)
    name = container_name or f"multidetect-sim-session-{uuid.uuid4().hex[:8]}"
    plan = build_docker_simulator_plan(
        scenario,
        repository_root=ROOT,
        job_dir=ROOT / "tmp" / "sim-session" / name,
        container_name=name,
        control_port=control_port,
        telemetry_port=telemetry_port,
        image=PINNED_IMAGE,
        dry_run=False,
    )
    command = list(plan.command)
    _replace_world_mount(command, world_path)
    image_index = command.index(plan.image)
    env = [
        "HEADLESS=0",
        "DISPLAY=:0",
        "WAYLAND_DISPLAY=wayland-0",
        "XDG_RUNTIME_DIR=/mnt/wslg/runtime-dir",
    ]
    if viewer == "wslg":
        env.extend(("GDK_BACKEND=x11", "QT_X11_NO_MITSHM=1"))
    if gcs_port is not None:
        if not 1024 <= gcs_port <= 65535 or gcs_port == control_port:
            raise ValueError("gcs-port must be in 1024..65535 and differ from control port")
        env.append(f"PX4_GCS_PORT={gcs_port}")
    # Replace the planner's default HEADLESS=1 and add the WSLg variables.
    for index in range(0, image_index - 1):
        if command[index] == "--env" and command[index + 1] == "HEADLESS=1":
            command[index + 1] = "HEADLESS=0"
            break
    for item in reversed([value for value in env if value != "HEADLESS=0"]):
        command[image_index:image_index] = ["--env", item]
    mount_at = command.index("--workdir")
    gui_mounts = [
        ("type=bind,source=/run/desktop/mnt/host/wslg/.X11-unix,target=/tmp/.X11-unix,readonly"),
        ("type=bind,source=/run/desktop/mnt/host/wslg,target=/mnt/wslg,readonly"),
    ]
    for spec in reversed(gui_mounts):
        command[mount_at:mount_at] = ["--mount", spec]
    if Path("/dev/dxg").exists():
        command[mount_at:mount_at] = ["--device", "/dev/dxg"]
        if Path("/usr/lib/wsl/lib").is_dir():
            command[mount_at:mount_at] = [
                "--mount",
                "type=bind,source=/usr/lib/wsl/lib,target=/usr/lib/wsl/lib,readonly",
            ]
    else:
        command[image_index:image_index] = ["--env", "LIBGL_ALWAYS_SOFTWARE=1"]
    if gcs_port is not None:
        command[image_index:image_index] = ["--publish", f"127.0.0.1:{gcs_port}:{gcs_port}/udp"]
        startup = command[-1]
        command[-1] = startup.replace(
            "exec /opt/px4-gazebo/bin/px4",
            f"printf '\\nmavlink start -x -u {gcs_port}\\n' >> \"$RC\"; "
            "exec /opt/px4-gazebo/bin/px4",
        )
    return command, plan, world_path


def _heartbeat(port: int, timeout: float = 20.0) -> bool:
    try:
        from pymavlink import mavutil
    except ImportError:
        return False
    try:
        connection = mavutil.mavlink_connection(f"udpin:127.0.0.1:{port}", autoreconnect=False)
        message = connection.wait_heartbeat(timeout=timeout)
    except OSError as exc:
        print(f"PX4 heartbeat socket unavailable on UDP {port}: {exc}", file=sys.stderr)
        return False
    return message is not None


def _run_task(task: str, world: Path, port: int, out: Path) -> int:
    """Run the existing onboard policy entry point against the session port."""
    out.mkdir(parents=True, exist_ok=True)
    task_file = out / "onboard-task.json"
    task_file.write_text(
        json.dumps({"task": task, "world": str(world)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    # The policy-chain command is the repository's model/admission smoke.  A
    # full live episode additionally needs the camera stream and mission
    # package; those are intentionally not fabricated here.
    command = [
        sys.executable,
        str(ROOT / "scripts/run_onboard_policy_chain.py"),
        "--output",
        str(out / "policy-chain.json"),
    ]
    try:
        return subprocess.run(command, cwd=ROOT, check=False).returncode
    except OSError as exc:
        print(f"onboard task could not start: {exc}", file=sys.stderr)
        return 2


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world", default=DEFAULT_WORLD)
    parser.add_argument("--airframe", default="x500_mono_cam")
    parser.add_argument("--gcs-port", type=int, default=None)
    parser.add_argument("--onboard-task")
    parser.add_argument("--onboard-out", type=Path, default=ROOT / "tmp/sim-session/onboard")
    parser.add_argument("--viewer", choices=("wslg", "novnc"), default="wslg")
    parser.add_argument("--container-name")
    parser.add_argument("--control-port", type=int, default=14540)
    parser.add_argument("--telemetry-port", type=int, default=14541)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.viewer == "novnc":
        parser.error("novnc fallback is not installed; use --viewer wslg")
    try:
        command, plan, world = build_session_command(
            world=args.world,
            airframe=args.airframe,
            gcs_port=args.gcs_port,
            viewer=args.viewer,
            container_name=args.container_name,
            control_port=args.control_port,
            telemetry_port=args.telemetry_port,
        )
    except (OSError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    docker = plan.docker_executable or shutil.which("docker")
    if not docker:
        parser.error("Docker Desktop executable not found")
    command[0] = docker
    print(f"Gazebo world: {world}")
    print(
        f"PX4 control UDP: 127.0.0.1:{args.control_port}; "
        f"telemetry: 127.0.0.1:{args.telemetry_port}"
    )
    if args.gcs_port:
        print(f"GCS UDP: 127.0.0.1:{args.gcs_port}")
    print("WSLg: DISPLAY=:0, WAYLAND_DISPLAY=wayland-0")
    try:
        subprocess.run(command, cwd=ROOT, check=True)
    except (OSError, subprocess.CalledProcessError):
        subprocess.run([docker, "stop", "--timeout", "2", plan.container_name], check=False)
        subprocess.run([docker, "rm", plan.container_name], check=False)
        raise
    try:
        for _ in range(60):
            state = subprocess.run(
                [docker, "inspect", "-f", "{{.State.Running}}", plan.container_name],
                capture_output=True,
                text=True,
                check=False,
            )
            if state.stdout.strip() == "true":
                break
            time.sleep(1)
        heartbeat_port = args.gcs_port or args.telemetry_port
        print(
            f"PX4 heartbeat: {'received' if _heartbeat(heartbeat_port) else 'not observed'} "
            f"on UDP {heartbeat_port}"
        )
        if args.onboard_task:
            code = _run_task(args.onboard_task, world, args.control_port, args.onboard_out)
            if code:
                return code
        print("Interactive session is running. Press Ctrl+C to stop.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping owned simulator container...")
    finally:
        subprocess.run([docker, "stop", "--timeout", "2", plan.container_name], check=False)
        subprocess.run([docker, "rm", plan.container_name], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_session_command", "resolve_world", "main"]
