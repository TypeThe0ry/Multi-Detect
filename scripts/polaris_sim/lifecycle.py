#!/usr/bin/env python3
"""Lease slots and manage the Polaris simulator's transient systemd units."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import suppress
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from scripts.sitl_lock import SitlLock  # noqa: E402

WORLD_DIR = ROOT / "simulation/gazebo/worlds"
DEFAULT_CITY_WORLD_DIR = Path(
    "/data/projects/Multi-Detect-env/runs/cities-recovery-20261109/worlds"
)
DEFAULT_WORLD = "rw_singapore_marina_bay"
SCENARIO_ROOT = ROOT / "configs/scenarios/realworld"
STATE_PATH = Path(os.environ.get("POLARIS_SIM_STATE_FILE", "/data/scratch/polaris-sim/state.json"))
SESSION_UNIT = "psim-session"
GATEWAY_UNIT = "psim-gateway"
VIDEO_UNIT = "psim-video"  # Stop an older separately managed video service during cleanup.
AI_UNIT = "psim-ai"


class LifecycleError(RuntimeError):
    pass


def _state_path() -> Path:
    return Path(os.environ.get("POLARIS_SIM_STATE_FILE", str(STATE_PATH)))


def _read_state() -> dict[str, object]:
    try:
        value = json.loads(_state_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise LifecycleError(f"invalid state file: {_state_path()}")
    return value


def _write_state(value: dict[str, object]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _world_dirs() -> list[Path]:
    configured = [
        Path(value.strip()).expanduser()
        for value in os.environ.get("POLARIS_SIM_WORLD_DIRS", "").split(":")
        if value.strip()
    ]
    paths = [WORLD_DIR, *configured, DEFAULT_CITY_WORLD_DIR]
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path if path.is_absolute() else ROOT / path
        resolved = resolved.resolve(strict=False)
        if resolved not in seen:
            unique.append(resolved)
            seen.add(resolved)
    return unique


def _world_sdfs(weather: str) -> list[Path]:
    return sorted(
        path
        for directory in _world_dirs()
        if directory.is_dir()
        for path in directory.glob(f"rw_*__{weather}.sdf")
    )


def _resolve_world(world: str | None, weather: str) -> tuple[str | None, Path | None]:
    requested = world or DEFAULT_WORLD
    supplied_path = Path(requested).expanduser()
    if supplied_path.suffix.lower() == ".sdf" and supplied_path.is_file():
        if supplied_path.is_symlink():
            raise LifecycleError(f"world SDF must not be a symlink: {supplied_path}")
        path = supplied_path.resolve()
        return path.stem.split("__", 1)[0], path
    stem = supplied_path.stem
    filename = stem if "__" in stem else f"{stem}__{weather}"
    for directory in _world_dirs():
        candidate = directory / f"{filename}.sdf"
        if candidate.is_file():
            if candidate.is_symlink():
                raise LifecycleError(f"world SDF must not be a symlink: {candidate}")
            return stem.split("__", 1)[0], candidate.resolve()
    return stem.split("__", 1)[0], None


def _managed_docker_command(command: list[str] | tuple[str, ...]) -> list[str]:
    result = list(command)
    result.remove("-d")
    result.insert(result.index("run") + 1, "--pull=never")
    # real-terrain worlds with a 720p camera exceed 12 GiB in gz-sim (OOM seen on rw_singapore_marina_bay)
    result.insert(result.index("--name"), f"--memory={os.environ.get('POLARIS_SIM_CONTAINER_MEMORY', '16g')}")
    result.insert(result.index("--name"), "--rm")
    return result


def _docker_plan_for_slot(
    *,
    scenario: Path,
    world_path: Path,
    slot: int,
    image: str,
    gpu: str,
    job_dir: Path,
):
    from scripts.run_simulator_docker import build_docker_simulator_plan

    return build_docker_simulator_plan(
        scenario,
        repository_root=ROOT,
        job_dir=job_dir,
        container_name=f"md-polaris-sim-slot-{slot}",
        control_port=14560 + 10 * slot,
        telemetry_port=14561 + 10 * slot,
        image=image,
        gpu_render=gpu != "none",
        gpu_device=gpu if gpu != "none" else None,
        gpu_devices=(gpu,) if gpu != "none" else None,
        worker_index=slot - 5,
        world_sdf_path=world_path,
    )


def _scenario_path(world: str, weather: str, airframe: str) -> Path:
    filename = f"{world}__{weather}.scenario.json"
    candidates = [SCENARIO_ROOT / filename]
    if airframe != "x500_mono_cam":
        candidates.insert(0, SCENARIO_ROOT / airframe / filename)
    return next((path for path in candidates if path.is_file()), candidates[0])


def _generation_command(world: str | None, weather: str) -> str:
    if world:
        site = Path(world).stem.split("__", 1)[0]
        return (
            f"python scripts/realworld/make_world.py {shlex.quote(site)} "
            f"--weather {shlex.quote(weather)}"
        )
    return (
        f"python scripts/realworld/make_world.py {DEFAULT_WORLD} "
        f"--weather {shlex.quote(weather)}"
    )


def _systemd_run(
    unit: str,
    command: list[str],
    memory: str,
    *,
    no_block: bool = True,
    env: dict[str, str] | None = None,
) -> None:
    argv = ["systemd-run", f"--unit={unit}", "--collect", f"--property=MemoryMax={memory}",
            "--property=KillMode=mixed", "--property=Restart=on-failure", "--property=RestartSec=3s"]
    for key, value in (env or {}).items():
        argv.append(f"--setenv={key}={value}")
    if no_block:
        argv.append("--no-block")
    argv.extend(["--", *command])
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise LifecycleError(f"systemd-run {unit} failed: {detail}")


def _systemctl(action: str, unit: str) -> bool:
    result = subprocess.run(["systemctl", action, unit], capture_output=True, text=True, check=False)
    return result.returncode == 0


def _select_gpus(value: str, count: int, *, dry_run: bool) -> list[str]:
    if value == "none":
        return ["none"] * count
    if value != "auto":
        if not value.isdigit():
            raise LifecycleError("--gpu must be auto, none, or a GPU index")
        selected = str(int(value))
        if selected in {"0", "3"}:
            raise LifecycleError("GPU 0 is reserved for the streaming desktop and GPU 3 is unstable; use GPU 1 or 2")
        if selected not in {"1", "2"}:
            raise LifecycleError("only workstation GPUs 1 and 2 are eligible for Polaris SITL")
        return [selected] * count
    try:
        apps = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory,gpu_uuid", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,uuid,memory.used,memory.total,utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        if dry_run:
            return ["auto"] * count
        raise LifecycleError("nvidia-smi is unavailable; auto GPU selection cannot proceed")
    if apps.returncode or result.returncode:
        if dry_run:
            return ["auto"] * count
        detail = (apps.stderr or apps.stdout or result.stderr or result.stdout).strip()
        raise LifecycleError(f"cannot select a render GPU: {detail}")
    app_memory_by_uuid: dict[str, int] = {}
    for line in apps.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 3 or parts[1].upper() == "N/A":
            continue
        try:
            app_memory_by_uuid[parts[2]] = app_memory_by_uuid.get(parts[2], 0) + int(parts[1])
        except ValueError:
            continue
    candidates = []
    for line in result.stdout.splitlines():
        try:
            raw_index, uuid, raw_used, raw_total, raw_utilization = (part.strip() for part in line.split(","))
            index, used, total, utilization = int(raw_index), int(raw_used), int(raw_total), int(raw_utilization)
        except ValueError:
            continue
        if index in {1, 2}:
            used = max(used, app_memory_by_uuid.get(uuid, 0))
            candidates.append({"index": index, "total": max(total, 1), "used": used, "utilization": utilization})
    if not candidates:
        if dry_run:
            return ["auto"] * count
        raise LifecycleError("auto GPU selection found no eligible GPU 1 or 2")
    assigned: dict[int, int] = {int(item["index"]): 0 for item in candidates}
    selected_gpus = []
    for _ in range(count):
        choice = min(
            candidates,
            key=lambda item: (
                (int(item["used"]) + assigned[int(item["index"])] * 4000) / int(item["total"]),
                int(item["utilization"]) + assigned[int(item["index"])] * 5,
                int(item["index"]),
            ),
        )
        index = int(choice["index"])
        selected_gpus.append(str(index))
        assigned[index] += 1
    return selected_gpus


def _local_image(gpu: bool) -> str:
    tags = (
        ["multidetect/px4-sitl-gazebo:gz-rendering-830-r1", "multidetect/px4-sitl-gazebo:transport-frame-09266eb0eb38"]
        if gpu else ["px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8"]
    )
    for tag in tags:
        result = subprocess.run(["docker", "image", "inspect", tag], capture_output=True, text=True, timeout=5, check=False)
        if result.returncode == 0:
            return tag
    raise LifecycleError(f"no approved local {'GPU' if gpu else 'CPU'} SITL image found; checked {', '.join(tags)}")


def _dry_run(args: argparse.Namespace) -> int:
    world, world_path = _resolve_world(args.world, args.weather)
    gpus = _select_gpus(args.gpu, args.vehicles, dry_run=True)
    slots = [
        {"slot": slot, "sysid": slot - 4, "gpu": gpus[index],
         "control_udp": 14560 + 10 * slot, "telemetry_udp": 14561 + 10 * slot}
        for index, slot in enumerate(range(5, 5 + args.vehicles))
    ]
    docker_image: str | None = None
    docker_image_note: str | None = None
    if "auto" not in gpus and world_path is not None:
        try:
            docker_image = _local_image(any(gpu != "none" for gpu in gpus))
        except (LifecycleError, OSError) as exc:
            docker_image = (
                "multidetect/px4-sitl-gazebo:gz-rendering-830-r1"
                if any(gpu != "none" for gpu in gpus)
                else "px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8"
            )
            docker_image_note = f"local image availability not verified: {exc}"
    if docker_image is not None and world is not None and world_path is not None:
        scenario = _scenario_path(world, args.weather, args.airframe)
        old_allow = os.environ.get("MULTIDETECT_SIM_ALLOW_GPUS")
        old_devices = os.environ.get("MULTIDETECT_SIM_GPU_DEVICES")
        selected_gpus = sorted({gpu for gpu in gpus if gpu != "none"})
        if selected_gpus:
            os.environ["MULTIDETECT_SIM_ALLOW_GPUS"] = ",".join(selected_gpus)
            os.environ["MULTIDETECT_SIM_GPU_DEVICES"] = ",".join(selected_gpus)
        try:
            for row in slots:
                if not scenario.is_file():
                    row["docker_command_error"] = f"scenario is missing: {scenario}"
                    continue
                if row["gpu"] == "auto":
                    row["docker_command_error"] = "GPU assignment unresolved"
                    continue
                try:
                    docker_plan = _docker_plan_for_slot(
                        scenario=scenario,
                        world_path=world_path,
                        slot=int(row["slot"]),
                        image=docker_image,
                        gpu=str(row["gpu"]),
                        job_dir=Path("/tmp/polaris-sim-dry-run") / f"slot-{row['slot']}",
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    row["docker_command_error"] = str(exc)
                else:
                    command = _managed_docker_command(docker_plan.command)
                    row["docker_command"] = command
                    row["docker_command_text"] = shlex.join(command)
        finally:
            if old_allow is None:
                os.environ.pop("MULTIDETECT_SIM_ALLOW_GPUS", None)
            else:
                os.environ["MULTIDETECT_SIM_ALLOW_GPUS"] = old_allow
            if old_devices is None:
                os.environ.pop("MULTIDETECT_SIM_GPU_DEVICES", None)
            else:
                os.environ["MULTIDETECT_SIM_GPU_DEVICES"] = old_devices
    else:
        for row in slots:
            if world_path is None:
                row["docker_command_error"] = "world SDF not found"
            elif row["gpu"] == "auto":
                row["docker_command_error"] = "GPU assignment unresolved"
            else:
                row["docker_command_error"] = "Docker image unresolved"
    plan = {
        "mode": "dry-run",
        "world": world,
        "weather": args.weather,
        "world_sdf": None if world_path is None else str(world_path),
        "world_generation_command": None if world_path else _generation_command(args.world, args.weather),
        "airframe": args.airframe,
        "gpu_assignments": gpus,
        "gpu_note": "nvidia-smi unavailable; auto selection will happen on launch" if "auto" in gpus else None,
        "slots": slots,
        "docker_image": docker_image,
        "docker_image_note": docker_image_note,
        "units": [SESSION_UNIT, GATEWAY_UNIT,
                  *(f"psim-sitl-slot-{row['slot']}" for row in slots),
                  *(unit for unit, script in ((AI_UNIT, ROOT / "scripts/polaris_sim/ai.py"),) if script.is_file())],
        "ports": {"mavlink_tcp": 5770, "mavlink_udp": 14550, "http": 8780, "discovery": "239.255.77.77:8781", "rtsp": 8554, "ai": 8790},
        "state_file": str(_state_path()),
    }
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


def _command_up(args: argparse.Namespace) -> int:
    if not 1 <= args.vehicles <= 3:
        raise LifecycleError("--vehicles must be 1, 2 or 3")
    if args.dry_run:
        return _dry_run(args)
    state = _read_state()
    if state.get("phase") == "running" and _systemctl("is-active", str(state.get("session_unit", SESSION_UNIT))):
        raise LifecycleError("a Polaris SIM session is already active; run down.sh first")
    world, world_path = _resolve_world(args.world, args.weather)
    if world is None or world_path is None:
        raise LifecycleError(f"real-world SDF is missing. Generate it with: {_generation_command(args.world, args.weather)}")
    for slot in range(5, 5 + args.vehicles):
        scenario = _scenario_path(world, args.weather, args.airframe)
        if not scenario.is_file():
            raise LifecycleError(f"scenario is missing: {scenario}; generate it with: {_generation_command(world, args.weather)}")
    gpus = _select_gpus(args.gpu, args.vehicles, dry_run=False)
    image = _local_image(any(gpu != "none" for gpu in gpus))
    arguments = [
        "supervise", "--world", world, "--weather", args.weather, "--airframe", args.airframe,
        "--world-sdf", str(world_path), "--vehicles", str(args.vehicles),
        "--gpus", ",".join(gpus), "--image", image,
    ]
    _systemd_run(SESSION_UNIT, [sys.executable, str(Path(__file__).resolve()), *arguments], "512M")
    print(f"启动请求已提交到 {SESSION_UNIT}；等待 SITL 心跳与 GPS fix。")
    deadline = time.monotonic() + args.ready_timeout
    last_error = "gateway not ready"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8780/v1/vehicles", timeout=1.0) as response:
                data = json.load(response)
            rows = data.get("vehicles", [])
            try:
                state = _read_state()
            except (OSError, LifecycleError):
                state = {}
            expected = (
                len(state.get("slots", []))
                if state.get("phase") == "running" and isinstance(state.get("slots"), list)
                else args.vehicles
            )
            ready = len(rows) == expected and expected > 0 and all(
                row.get("last_heartbeat_age_s") is not None
                and row["last_heartbeat_age_s"] < 5
                and (row.get("gps_fix_type") or 0) >= 3 for row in rows
            )
            if ready:
                host = rows and _host_address() or "192.168.1.138"
                for warning in state.get("warnings", []) if isinstance(state.get("warnings", []), list) else []:
                    print(f"WARN: {warning}")
                uri = f"polaris-sim://{host}?mavlink=tcp:{host}:5770&http={host}:8780&rtsp={host}:8554&ai={host}:8790"
                print(f"Polaris MAVLink TCP: {host}:5770 (Android 默认)；UDP: {host}:14550")
                for index, row in enumerate(rows):
                    suffixes = ("cam", "gimbal") if "gimbal" in str(row.get("airframe", "")) else ("cam",)
                    for suffix in suffixes:
                        print(f"sysid {row['sysid']} 视频: rtsp://{host}:8554/v{index}/{suffix}")
                print(f"AI: http://{host}:8790；控制: http://{host}:8780")
                print(f"二维码文本: {uri}")
                return 0
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last_error = str(exc)
        time.sleep(1)
    _systemctl("stop", SESSION_UNIT)
    raise LifecycleError(f"SITL readiness timeout after {args.ready_timeout}s: {last_error}; owned units were stopped")


def _host_address() -> str:
    result = subprocess.run([sys.executable, "-c", "from multidetect.polaris_sim.gateway.server import discover_lan_address; print(discover_lan_address())"],
                            cwd=ROOT, capture_output=True, text=True, timeout=3, check=False,
                            env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else "192.168.1.138"


async def _supervise(args: argparse.Namespace) -> int:
    world, world_path = _resolve_world(args.world, args.weather)
    if args.world_sdf:
        supplied_world_path = Path(args.world_sdf).expanduser()
        if (
            supplied_world_path.is_symlink()
            or not supplied_world_path.is_file()
            or supplied_world_path.stem.split("__", 1)[0] != world
        ):
            raise LifecycleError(f"world SDF does not match selected world: {supplied_world_path}")
        world_path = supplied_world_path.resolve()
    if world is None or world_path is None:
        raise LifecycleError(f"missing SDF; generate it with: {_generation_command(args.world, args.weather)}")
    desired = list(range(5, 5 + args.vehicles))
    gpu_assignments = args.gpus.split(",")
    if len(gpu_assignments) != args.vehicles or any(value not in {"none", "1", "2"} for value in gpu_assignments):
        raise LifecycleError("--gpus must provide one value per vehicle from none, 1 or 2")
    locks: list[SitlLock] = []
    units: list[str] = []
    state: dict[str, object] = {"phase": "starting", "session_unit": SESSION_UNIT, "pid": os.getpid(),
                                "started_at": time.time(), "world": world, "weather": args.weather,
                                "airframe": args.airframe, "slots": [], "units": []}
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop_event.set)
    try:
        os.environ["MULTIDETECT_SITL_SLOTS"] = "8"
        selected_gpus = sorted({gpu for gpu in gpu_assignments if gpu != "none"})
        if selected_gpus:
            os.environ["MULTIDETECT_SIM_ALLOW_GPUS"] = ",".join(selected_gpus)
            os.environ["MULTIDETECT_SIM_GPU_DEVICES"] = ",".join(selected_gpus)
        for slot in desired:
            lock = SitlLock(label=f"polaris-sim-slot-{slot}", slots=8, slot=slot, retry_s=3.0)
            try:
                lock.acquire(wait=False)
            except RuntimeError as exc:
                if slot == 7 and args.vehicles == 3:
                    state.setdefault("warnings", []).append(f"slot 7 unavailable; starting slots 5 and 6: {exc}")
                    break
                raise LifecycleError(f"cannot lease slot {slot}: {exc}") from exc
            locks.append(lock)
        if not locks:
            raise LifecycleError("no SITL slots were leased")
        for lock in locks:
            slot = int(lock.slot)
            control = 14560 + 10 * slot
            telemetry = control + 1
            scenario = _scenario_path(world, args.weather, args.airframe)
            if not scenario.is_file():
                raise LifecycleError(f"scenario missing: {scenario}")
            selected_gpu = gpu_assignments[slot - 5]
            job_dir = _state_path().parent / "jobs" / f"slot-{slot}"
            plan = _docker_plan_for_slot(
                scenario=scenario,
                world_path=world_path,
                slot=slot,
                image=args.image,
                gpu=selected_gpu,
                job_dir=job_dir,
            )
            command = _managed_docker_command(plan.command)
            name = f"md-polaris-sim-slot-{slot}"
            unit = f"psim-sitl-slot-{slot}"
            _systemd_run(unit, command, "12G")
            units.append(unit)
            state["slots"].append({"slot": slot, "sysid": slot - 4, "container": name,
                                   "control_port": control, "telemetry_port": telemetry,
                                   "scenario": str(scenario), "model_name": plan.model_name,
                                   "worker_index": plan.worker_index})
        slot_args = ",".join(str(lock.slot) for lock in locks)
        gateway_cmd = [sys.executable, str(ROOT / "scripts/polaris_sim/gateway.py"),
                       "--slots", slot_args, "--world", world, "--airframe", args.airframe,
                       "--host-address", _host_address(), "--state-file", str(_state_path()),
                       "--record-dir", str(_state_path().parent / "recordings")]
        camera_name = "gimbal" if "gimbal" in args.airframe else "cam"
        for row in [] if getattr(args, "no_video", False) else state["slots"]:
            gateway_cmd.extend(
                [
                    "--video-source",
                    str(row["slot"]),
                    camera_name,
                    str(row["container"]),
                    world,
                    f"{row['model_name']}_{row['worker_index']}",
                ]
            )
        if getattr(args, "no_video", False):
            gateway_cmd.append("--no-video")
        _systemd_run(GATEWAY_UNIT, gateway_cmd, "1G")
        units.append(GATEWAY_UNIT)
        vehicle_env = json.dumps(
            [{"slot": int(lock.slot), "sysid": int(lock.slot) - 4,
              "world": world, "airframe": args.airframe,
              "control_port": 14560 + 10 * int(lock.slot),
              "telemetry_port": 14561 + 10 * int(lock.slot),
              "rtsp": f"rtsp://{_host_address()}:8554/v{index}/{'gimbal' if 'gimbal' in args.airframe else 'cam'}"}
             for index, lock in enumerate(locks)],
            separators=(",", ":"),
        )
        lane_env = {
            "PYTHONPATH": f"{ROOT / 'src'}:{ROOT}",
            "POLARIS_SIM_HOST": _host_address(),
            "POLARIS_SIM_HTTP_URL": "http://127.0.0.1:8780",
            "POLARIS_SIM_AI_URL": "http://127.0.0.1:8790",
            "POLARIS_SIM_VIDEO_ENCODER": "libx264",
            "POLARIS_SIM_VEHICLES": vehicle_env,
            "POLARIS_SIM_RECORD_DIR": str(_state_path().parent / "recordings"),
        }
        for unit, script in ((AI_UNIT, ROOT / "scripts/polaris_sim/ai.py"),):
            if script.is_file():
                _systemd_run(unit, [sys.executable, str(script)], "4G", env=lane_env)
                units.append(unit)
            else:
                print(f"Polaris SIM: 未发现 {script.relative_to(ROOT)}，跳过 {unit}。", flush=True)
        state["units"] = units
        state["gpu_assignments"] = gpu_assignments[: len(locks)]
        state["phase"] = "running"
        _write_state(state)
        print(json.dumps({"status": "running", **state}, ensure_ascii=False), flush=True)
        await stop_event.wait()
    except BaseException:
        state["phase"] = "failed"
        state["units"] = units
        with suppress(OSError):
            _write_state(state)
        raise
    finally:
        for unit in reversed(units):
            _systemctl("stop", unit)
        for lock in reversed(locks):
            lock.release()
        state["phase"] = "stopped"
        state["stopped_at"] = time.time()
        state["units"] = units
        with suppress(OSError):
            _write_state(state)
    return 0


def _down() -> int:
    state = _read_state()
    units = state.get("units", [])
    if not isinstance(units, list):
        raise LifecycleError("state file contains an invalid unit list")
    allowed = {SESSION_UNIT, GATEWAY_UNIT, VIDEO_UNIT, AI_UNIT}
    allowed.update(f"psim-sitl-slot-{slot}" for slot in (5, 6, 7))
    stopped = []
    for unit in [*reversed(units), SESSION_UNIT]:
        if isinstance(unit, str) and unit in allowed and unit not in stopped:
            _systemctl("stop", unit)
            stopped.append(unit)
    state["phase"] = "stopped"
    state["stopped_at"] = time.time()
    state["units"] = stopped
    with suppress(OSError):
        _write_state(state)
    print(json.dumps({"status": "stopped", "units": stopped}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("up", "supervise"):
        command = sub.add_parser(name)
        command.add_argument("--world", default=None)
        if name == "supervise":
            command.add_argument("--world-sdf", type=Path)
        command.add_argument("--weather", default="clear_noon")
        command.add_argument("--vehicles", type=int, choices=(1, 2, 3), default=1)
        command.add_argument("--no-video", action="store_true", help="do not subscribe to the Gazebo camera")
        command.add_argument("--airframe", default="x500_mono_cam")
        if name == "up":
            command.add_argument("--gpu", default="auto")
        else:
            command.add_argument("--gpus", required=True)
        command.add_argument("--image", default=None)
        command.add_argument("--ready-timeout", type=int, default=300)
        command.add_argument("--dry-run", action="store_true")
    sub.add_parser("down")
    args = parser.parse_args(argv)
    try:
        if args.command == "up":
            return _command_up(args)
        if args.command == "supervise":
            if args.image is None:
                raise LifecycleError("supervise requires a preselected local image")
            return asyncio.run(_supervise(args))
        return _down()
    except (LifecycleError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"polaris-sim: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
