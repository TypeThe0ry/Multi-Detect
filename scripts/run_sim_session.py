#!/usr/bin/env python3
"""Start an interactive PX4/Gazebo session in Docker.

The session owns one container and removes it on Ctrl+C.  It deliberately
reuses ``run_simulator_docker`` for the pinned image and repository asset
contract, then adds the container-side browser viewer.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from multidetect.provenance import build_provenance
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scripts.run_simulator_docker import (  # noqa: E402
    PINNED_IMAGE,
    DockerSimulatorPlan,
    build_docker_simulator_plan,
)
from scripts.platform_paths import default_llama_server
from scripts.sitl_lock import SitlLock, default_lock_path, sitl_lock_context
from multidetect.sim_network import sim_listen_host, sim_udpin_endpoint

DEFAULT_WORLD = "urban_inspection_multirotor_001.sdf"
DEFAULT_AIRFRAME_WORLDS = {
    "x500": DEFAULT_WORLD,
    "x500_mono_cam": DEFAULT_WORLD,
    "x500_gimbal": DEFAULT_WORLD,
    "standard_vtol": "river_rescue_vtol_001.sdf",
    "quadtailsitter": "quadtailsitter_mono_cam_001.sdf",
    "tiltrotor": "tiltrotor_mono_cam_001.sdf",
    "rc_cessna": "coastal_patrol_fixed_wing_001.sdf",
    "rc_cessna_mono_cam": "coastal_patrol_fixed_wing_001.sdf",
}
BASE_IMAGE_SHORT = PINNED_IMAGE.split("sha256:", 1)[1][:12]
VIEWER_IMAGE = f"multidetect/px4-gazebo-viewer:{BASE_IMAGE_SHORT}"
RESOLVED_FUEL_CACHE = ROOT / "artifacts/cache/gz-fuel/resolved"
DEFAULT_LLAMA_SERVER = default_llama_server(ROOT)
# Onboard-policy defaults for --onboard-task (the r18 mission-suite defaults).
# Values starting with "artifacts/" resolve against --artifacts-root.
DEFAULT_ONBOARD_DEFAULTS = ROOT / "configs/onboard/sim-session-r18-defaults.json"
DEFAULT_ARTIFACTS_ROOT = ROOT / "artifacts"
# Onboard camera frames at the suite's (and the x500 camera calibration's) size.
SESSION_RGB_SIZE = "1280x960"
SCENARIO_INDEXES = ROOT / "configs/scenarios"
WORLD_SCENARIOS = {
    "urban_inspection_multirotor_001.sdf": ROOT
    / "configs/scenarios/urban_inspection_multirotor_001.scenario.json",
    "river_rescue_vtol_001.sdf": ROOT / "configs/scenarios/river_rescue_vtol_001.scenario.json",
    "coastal_patrol_fixed_wing_001.sdf": ROOT
    / "configs/scenarios/coastal_patrol_fixed_wing_001.scenario.json",
}
WORLD_SCENARIOS.update(
    {
        "gen_cnt2_w0000.sdf": ROOT
        / "configs/scenarios/generated-count-site/gen_cnt2_w0000.scenario.json",
    }
)
AIRFRAME_SCENARIOS = {
    "x500": WORLD_SCENARIOS["urban_inspection_multirotor_001.sdf"],
    "x500_mono_cam": WORLD_SCENARIOS["urban_inspection_multirotor_001.sdf"],
    "x500_gimbal": WORLD_SCENARIOS["urban_inspection_multirotor_001.sdf"],
    "standard_vtol": WORLD_SCENARIOS["river_rescue_vtol_001.sdf"],
    "tiltrotor": ROOT / "configs/scenarios/airframes/tiltrotor_mono_cam_001.scenario.json",
    "quadtailsitter": ROOT
    / "configs/scenarios/airframes/quadtailsitter_mono_cam_001.scenario.json",
    "rc_cessna": WORLD_SCENARIOS["coastal_patrol_fixed_wing_001.sdf"],
    "rc_cessna_mono_cam": WORLD_SCENARIOS["coastal_patrol_fixed_wing_001.sdf"],
    "rc_cessna_runway": WORLD_SCENARIOS["coastal_patrol_fixed_wing_001.sdf"],
    "rc_cessna_hand": WORLD_SCENARIOS["coastal_patrol_fixed_wing_001.sdf"],
    "rc_cessna_catapult": WORLD_SCENARIOS["coastal_patrol_fixed_wing_001.sdf"],
}
WORLD_SCENARIOS.update(
    {
        "quadtailsitter_mono_cam_001.sdf": AIRFRAME_SCENARIOS["quadtailsitter"],
        "tiltrotor_mono_cam_001.sdf": AIRFRAME_SCENARIOS["tiltrotor"],
    }
)
VTOL_AIRFRAMES = ("standard_vtol", "quadtailsitter", "tiltrotor")
FIXED_WING_TAKEOFFS = ("runway", "hand", "catapult")


def _canonical_airframe(value: str) -> str:
    normalized = str(value).strip().lower().replace("-", "_")
    aliases = {
        "x500": "x500_mono_cam",
        "multirotor": "x500_mono_cam",
        "standard_vtol_mono_cam": "standard_vtol",
        "quadtailsitter_mono_cam": "quadtailsitter",
        "tiltrotor_mono_cam": "tiltrotor",
        "rc_cessna": "rc_cessna_mono_cam",
        "rc_cessna_runway": "rc_cessna_mono_cam",
        "rc_cessna_hand": "rc_cessna_mono_cam",
        "rc_cessna_catapult": "rc_cessna_mono_cam",
    }
    return aliases.get(normalized, normalized)


def _vehicle_family(value: str | None) -> str | None:
    """Coarse vehicle family of an airframe id or a runtime vehicle model."""
    text = str(value or "").lower()
    for token, family in (
        ("quadtailsitter", "quadtailsitter"),
        ("tiltrotor", "tiltrotor"),
        ("vtol", "standard_vtol"),
        ("cessna", "fixed_wing"),
        ("fixed_wing", "fixed_wing"),
        ("plane", "fixed_wing"),
        ("multirotor", "multirotor"),
        ("x500", "multirotor"),
    ):
        if token in text:
            return family
    return None


def resolve_takeoff_method(airframe: str, takeoff_method: str | None = None) -> str | None:
    """Collector takeoff method for an airframe; rejects methods it cannot fly.

    ``rc_cessna_hand`` / ``rc_cessna_catapult`` imply their launch method (the
    collector picks the PX4 launch profile from ``--takeoff-method``).
    """
    family = _vehicle_family(_canonical_airframe(airframe))
    normalized = str(airframe).strip().lower().replace("-", "_")
    implied = next(
        (method for method in ("hand", "catapult") if normalized.endswith(f"_{method}")), None
    )
    if family == "fixed_wing":
        allowed, default = FIXED_WING_TAKEOFFS, implied or "runway"
    elif family in VTOL_AIRFRAMES:
        allowed, default = ("vtol",), "vtol"
    elif family == "multirotor":
        allowed, default = (), None
    else:
        return takeoff_method
    if takeoff_method is None:
        return default
    if takeoff_method not in allowed or (implied and takeoff_method != implied):
        raise ValueError(
            f"takeoff method {takeoff_method!r} does not apply to airframe {airframe!r}"
            + (f"; use one of {', '.join(allowed)}" if allowed else "")
        )
    return takeoff_method


def resolve_world(value: str | Path) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        if candidate.suffix == "":
            candidate = candidate.with_suffix(".sdf")
        by_name = ROOT / "simulation/gazebo/worlds" / candidate
        airframe_world = ROOT / "configs/scenarios/airframes" / candidate
        candidate = (
            by_name
            if by_name.is_file()
            else (airframe_world if airframe_world.is_file() else ROOT / candidate)
        )
    candidate = candidate.resolve()
    if not candidate.is_file() or candidate.suffix.lower() != ".sdf":
        raise ValueError(f"world not found or not an SDF file: {value}")
    if ROOT not in candidate.parents:
        raise ValueError("world must be inside the repository")
    return candidate


@functools.lru_cache(maxsize=1)
def _indexed_worlds() -> dict[str, list[tuple[Path, dict]]]:
    """world_file -> [(scenario path, index entry)] over the scenario indexes.

    Covers ``configs/scenarios/*/index.json`` and the per-airframe derivations
    one level deeper (``vtol-v2/<airframe>/index.json``).
    """
    worlds: dict[str, list[tuple[Path, dict]]] = {}
    indexes = [*SCENARIO_INDEXES.glob("*/index.json"), *SCENARIO_INDEXES.glob("*/*/index.json")]
    for index in sorted(indexes):
        try:
            entries = json.loads(index.read_text(encoding="utf-8")).get("worlds") or []
        except (OSError, ValueError):
            continue
        for entry in entries:
            world_file = entry.get("world_file")
            if not world_file:
                continue
            relative = ((entry.get("files") or {}).get("scenario") or {}).get("path")
            scenario = index.parent / (relative or f"{Path(world_file).stem}.scenario.json")
            if scenario.is_file():
                worlds.setdefault(world_file, []).append((scenario, entry))
    return worlds


@functools.cache
def _scenario_vehicle_model(scenario: Path) -> str | None:
    """``runtime_input.vehicle.model`` of a scenario (the vehicle PX4 spawns)."""
    try:
        document = json.loads(scenario.read_text(encoding="utf-8"))
        channel = (document.get("channels") or {}).get("runtime_input") or {}
        runtime = json.loads((scenario.parent / channel["path"]).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    vehicle = runtime.get("vehicle") if isinstance(runtime, dict) else None
    return vehicle.get("model") if isinstance(vehicle, dict) else None


def _world_scenarios(world_name: str) -> list[Path]:
    """Every scenario written for a world: named, indexed, or a variant's base."""
    found = [WORLD_SCENARIOS[world_name]] if world_name in WORLD_SCENARIOS else []
    found += [scenario for scenario, _ in _indexed_worlds().get(world_name, [])]
    if not found and "__" in world_name:
        # augment_sim_world names variants <base>__<suffix>.sdf; an unindexed
        # variant keeps the base world's spawn pose and home.
        found = _world_scenarios(world_name.split("__", 1)[0] + ".sdf")
    return found


def _scenario_for(world: Path, airframe: str) -> Path:
    """Scenario for a session world, matching the airframe's vehicle.

    A world can carry several scenarios (generated-v2 and its fixed-wing /
    VTOL derivations); the one whose runtime vehicle is the session's airframe
    family wins.  A known world with no scenario for this airframe is an
    error: its spawn pose would launch a different vehicle.  An unknown world
    falls back to the airframe's own scenario.
    """
    canonical = _canonical_airframe(airframe)
    wanted = _vehicle_family(canonical)
    candidates = _world_scenarios(world.name)
    for scenario in candidates:
        family = _vehicle_family(_scenario_vehicle_model(scenario))
        if wanted is None or family is None or family == wanted:
            return scenario
    if candidates:
        raise ValueError(
            f"world {world.name!r} has no scenario for airframe {airframe!r}; "
            "pick a world made for it or omit --world to use the airframe default"
        )
    try:
        return AIRFRAME_SCENARIOS[canonical]
    except KeyError as exc:
        raise ValueError(
            f"airframe {airframe!r} has no known scenario; use x500, standard_vtol, "
            "quadtailsitter, tiltrotor, or rc_cessna"
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
    world: str | Path | None = None,
    airframe: str = "x500_mono_cam",
    gcs_port: int | None = None,
    viewer: str = "novnc",
    container_name: str | None = None,
    control_port: int = 14540,
    telemetry_port: int = 14541,
    viewer_port: int = 6080,
) -> tuple[list[str], DockerSimulatorPlan, Path]:
    """Build the Docker argv without starting Docker (unit-test boundary).

    ``world=None`` selects the airframe's default world.
    """
    canonical = _canonical_airframe(airframe)
    if world is None:
        world = DEFAULT_AIRFRAME_WORLDS.get(canonical, DEFAULT_WORLD)
    world_path = resolve_world(world)
    scenario = _scenario_for(world_path, canonical)
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
        airframe_profile_id=canonical,
    )
    command = list(plan.command)
    _replace_world_mount(command, world_path)
    image_index = command.index(plan.image)
    command[image_index] = VIEWER_IMAGE
    if viewer != "novnc":
        raise ValueError("the browser viewer is the only supported viewer")
    env = ["HEADLESS=1", "DISPLAY=:99", "LIBGL_ALWAYS_SOFTWARE=1", f"VIEWER_PORT={viewer_port}"]
    if gcs_port is not None:
        if not 1024 <= gcs_port <= 65535 or gcs_port == control_port:
            raise ValueError("gcs-port must be in 1024..65535 and differ from control port")
    for index in range(0, image_index - 1):
        if command[index] == "--env" and command[index + 1] == "HEADLESS=1":
            command[index + 1] = "HEADLESS=1"
            break
    for item in reversed([value for value in env if value != "HEADLESS=1"]):
        command[image_index:image_index] = ["--env", item]
    command[image_index:image_index] = ["--publish", f"127.0.0.1:{viewer_port}:{viewer_port}/tcp"]
    startup = command[-1]
    viewer_start = (
        "Xvfb :99 -screen 0 1280x720x24 -ac +extension GLX +render -noreset & "
        "x11vnc -display :99 -forever -shared -nopw -rfbport 5900 -quiet & "
        f"websockify --web=/usr/share/novnc {viewer_port} 127.0.0.1:5900 & "
        "(sleep 8; DISPLAY=:99 LIBGL_ALWAYS_SOFTWARE=1 "
        "gz sim -g --render-engine-gui ogre2 "
        "/opt/px4-gazebo/share/gz/worlds/default.sdf >/tmp/gz-gui.log 2>&1 "
        "|| DISPLAY=:99 LIBGL_ALWAYS_SOFTWARE=1 gz sim -g --render-engine-gui ogre "
        "/opt/px4-gazebo/share/gz/worlds/default.sdf >>/tmp/gz-gui.log 2>&1 || true) & "
    )
    command[-1] = startup.replace(
        "exec /opt/px4-gazebo/bin/px4", viewer_start + "exec /opt/px4-gazebo/bin/px4"
    )
    if gcs_port is not None:
        command[-1] = command[-1].replace(
            "exec /opt/px4-gazebo/bin/px4",
            _gcs_rc_append(gcs_port) + "exec /opt/px4-gazebo/bin/px4",
        )
    return command, plan, world_path


def _gcs_rc_append(gcs_port: int) -> str:
    """Shell that adds the GCS MAVLink instance to the container rc file once.

    A restarted container re-runs its start-up command on the same file system;
    an unconditional append would start a second instance on the same ports.
    """
    gcs_line = (
        f"mavlink start -x -t $DOCKER_HOST_IP -u 14542 -o {gcs_port} "
        "-r 4000000 -f $mavlink_network_interface_arg; "
    )
    return (
        f"grep -qF -- '-u 14542 -o {gcs_port} ' \"$RC\" || printf '\\n{gcs_line}\\n' >> \"$RC\"; "
    )


def _heartbeat(port: int, timeout: float = 20.0) -> bool:
    try:
        from pymavlink import mavutil
    except ImportError:
        return False
    try:
        connection = mavutil.mavlink_connection(sim_udpin_endpoint(port), autoreconnect=False)
    except OSError as exc:
        print(f"PX4 heartbeat socket unavailable on UDP {port}: {exc}", file=sys.stderr)
        return False
    try:
        return connection.wait_heartbeat(timeout=timeout) is not None
    except OSError as exc:
        print(f"PX4 heartbeat socket unavailable on UDP {port}: {exc}", file=sys.stderr)
        return False
    finally:
        connection.close()


def _heartbeat_details(port: int, timeout: float) -> dict[str, object] | None:
    try:
        from pymavlink import mavutil

        connection = mavutil.mavlink_connection(sim_udpin_endpoint(port), autoreconnect=False)
        try:
            message = connection.wait_heartbeat(timeout=timeout)
        finally:
            # Release the port: it is the GCS port a real ground station binds next.
            connection.close()
        if message is None:
            return None
        return {
            "sysid": int(message.get_srcSystem()),
            "compid": int(message.get_srcComponent()),
            "autopilot": int(getattr(message, "autopilot", -1)),
            "type": int(getattr(message, "type", -1)),
        }
    except (ImportError, OSError, ValueError) as exc:
        raise RuntimeError(f"MAVLink heartbeat listener failed on UDP {port}: {exc}") from exc


def _session_log(path: Path | None, phase: str, **fields: object) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": time.time(), "phase": phase, **fields}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _snapshot(
    docker: str, container: str, directory: Path, *, label: str | None = None
) -> Path | None:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    suffix = f"-{label}" if label else ""
    target = directory / f"{stamp}{suffix}.png"
    remote = f"/tmp/sim-session-{stamp}.png"
    capture = subprocess.run(
        [docker, "exec", container, "sh", "-lc", f"DISPLAY=:99 import -window root {remote}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if capture.returncode:
        return None
    copied = subprocess.run([docker, "cp", f"{container}:{remote}", str(target)], check=False)
    return target if copied.returncode == 0 else None


def _wait_world_ready(docker: str, container: str, timeout: float) -> tuple[bool, bool, str]:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        logs = subprocess.run(
            [docker, "logs", "--tail", "240", container],
            capture_output=True,
            text=True,
            check=False,
        )
        last = logs.stdout + logs.stderr
        ready = "Gazebo world is ready" in last or "World is ready" in last
        spawned = any(
            token in last.lower() for token in ("x500", "vehicle spawned", "spawned vehicle")
        )
        if ready or ("Starting gazebo" in last and spawned):
            return True, spawned, last
        time.sleep(1)
    return False, False, last


def ensure_viewer_image(docker: str, *, force: bool = False) -> None:
    """Build the cached viewer layer once; runtime sessions never apt-install."""
    present = (
        subprocess.run(
            [docker, "image", "inspect", VIEWER_IMAGE], capture_output=True, check=False
        ).returncode
        == 0
    )
    if present and not force:
        return
    subprocess.run(
        [
            docker,
            "build",
            "--pull=false",
            "-t",
            VIEWER_IMAGE,
            str(ROOT / "simulation/docker/viewer"),
        ],
        cwd=ROOT,
        check=True,
    )


def validate_runtime_prerequisites(*, onboard: bool = False) -> None:
    if not RESOLVED_FUEL_CACHE.is_dir():
        raise RuntimeError(
            "resolved Gazebo Fuel cache is missing: "
            f"{RESOLVED_FUEL_CACHE}. Create the repository artifacts "
            "asset cache before starting."
        )
    if onboard and not DEFAULT_LLAMA_SERVER.is_file():
        raise RuntimeError(
            "llama-server is missing: "
            f"{DEFAULT_LLAMA_SERVER}. Install or link the r18 artifacts before --onboard-task."
        )


def _operator_link_config(args) -> dict:
    """Session-local operator link keys, also written for the GCS console."""
    import secrets

    config = {
        "operator_bind_host": "127.0.0.1",
        "operator_port": args.operator_port,
        "operator_hmac_key_hex": os.environ.get("MULTIDETECT_SESSION_HMAC_HEX")
        or secrets.token_hex(32),
        "operator_mavlink_signing_key_hex": os.environ.get("MULTIDETECT_SESSION_SIGNING_HEX")
        or secrets.token_hex(32),
        # Onboard admits only signed packages; the console signs with this
        # session HMAC key (MultiDetectGCS signs with its Ed25519 key instead).
        "operator_package_key_hex": os.environ.get("MULTIDETECT_SESSION_PACKAGE_HEX")
        or secrets.token_hex(32),
        "operator_requires_approval": True,
    }
    args.onboard_out.mkdir(parents=True, exist_ok=True)
    (args.onboard_out / "operator-link.json").write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8"
    )
    print(
        "Operator link: send a prompt package with scripts/fake_gcs_console.py --link "
        f"{args.onboard_out / 'operator-link.json'} <package.json>"
    )
    return config


def _run_task(
    task: str,
    world: Path,
    port: int,
    out: Path,
    container_name: str,
    timeout_s: float = 2400.0,
    operator_link: dict | None = None,
    *,
    airframe: str = "x500_mono_cam",
    takeoff_method: str | None = None,
    telemetry_port: int = 14541,
    onboard_defaults: Path = DEFAULT_ONBOARD_DEFAULTS,
    artifacts_root: Path = DEFAULT_ARTIFACTS_ROOT,
) -> int:
    """Run the existing onboard policy entry point against the session port."""
    out.mkdir(parents=True, exist_ok=True)
    task_file = out / "onboard-task.json"
    task_file.write_text(
        json.dumps({"task": task, "world": str(world)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    canonical = _canonical_airframe(airframe)
    takeoff_method = resolve_takeoff_method(airframe, takeoff_method)
    scenario = _scenario_for(world, canonical)
    defaults = json.loads(Path(onboard_defaults).read_text(encoding="utf-8"))["defaults"]
    defaults["runtime_step_limit"] = 220
    for key, value in defaults.items():
        if isinstance(value, str) and value.startswith("artifacts/"):
            defaults[key] = str(Path(artifacts_root) / value.removeprefix("artifacts/"))
    if operator_link:
        # The mission comes from a GCS over the signed operator link (prompt →
        # onboard plan → approval); the policy holds until the plan is approved.
        defaults["mission"] = "operator_link"
        defaults.update(operator_link)
    config = out / "onboard-config.json"
    config.write_text(json.dumps(defaults, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    command = [
        sys.executable,
        str(ROOT / "scripts/collect_sim_teacher_episodes.py"),
        "--scenario",
        str(scenario),
        "--airframe",
        canonical,
        *(["--takeoff-method", takeoff_method] if takeoff_method else []),
        "--teacher",
        "onboard",
        "--attach-container",
        container_name,
        "--control-port",
        str(port),
        "--telemetry-port",
        str(telemetry_port),
        "--onboard-config",
        str(config),
        "--task-text",
        task,
        "--episodes",
        "1",
        "--steps-per-episode",
        # Room for coverage completion and the return-home step: a session count
        # found all three cones but hit a 140-step cap before finishing.
        "220",
        # The mission suite (eval_onboard_loop_campaign) and the camera's native
        # calibration are 1280x960.  The collector's 640x480 default halved every
        # box: e2e run10 saw the cones at 6-9 px, under the loop's 10 px verdict
        # floor, so two of three were never asked about and the count found one.
        "--rgb-size",
        SESSION_RGB_SIZE,
        # The collector's 300 s default ended a SITL count mid-search; the
        # mission suite gives the same task 2400 s.
        "--max-episode-s",
        str(timeout_s),
        *(
            # A supervised (operator-link) mission runs the onboard energy model;
            # PX4's simulated battery drains to 50 % in minutes and triggered an
            # "energy reserve reached" return mid-count in SITL.  Keep it charged
            # for interactive sessions; the suite's reserve-return mission keeps
            # the default drain.  PX4's SITL battery simulator is the same module
            # for multirotor, fixed-wing and VTOL airframes, so this holds for all.
            ("--sim-param", "SIM_BAT_MIN_PCT=95") if operator_link else ()
        ),
        # One session episode writes ~1 GB; the collector's 15 GB default guard is
        # sized for batch campaigns and aborted a session flight at 13 GB free.
        "--min-free-gb",
        "6",
        "--out",
        str(out / "collector"),
    ]
    try:
        return subprocess.run(command, cwd=ROOT, check=False).returncode
    except OSError as exc:
        print(f"onboard task could not start: {exc}", file=sys.stderr)
        return 2


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world", help=f"default: the airframe's world ({DEFAULT_WORLD} for x500)")
    parser.add_argument("--airframe", default="x500_mono_cam")
    parser.add_argument("--takeoff-method", choices=("runway", "hand", "catapult", "vtol"))
    parser.add_argument("--gcs-port", type=int, default=14560)
    parser.add_argument("--onboard-task")
    parser.add_argument("--exit-after-task", action="store_true")
    parser.add_argument("--onboard-out", type=Path, default=ROOT / "tmp/sim-session/onboard")
    parser.add_argument("--task-timeout-s", type=float, default=2400.0)
    parser.add_argument("--onboard-defaults", type=Path, default=DEFAULT_ONBOARD_DEFAULTS)
    parser.add_argument(
        "--artifacts-root",
        type=Path,
        default=DEFAULT_ARTIFACTS_ROOT,
        help='root that "artifacts/..." paths in --onboard-defaults resolve against',
    )
    parser.add_argument(
        "--operator-link",
        action="store_true",
        help="take the onboard mission from a GCS (scripts/fake_gcs_console.py or MultiDetectGCS)",
    )
    parser.add_argument("--operator-port", type=int, default=14580)
    parser.add_argument("--snapshot-dir", type=Path)
    parser.add_argument("--snapshot-interval", type=float, default=10.0)
    parser.add_argument("--check-heartbeat", type=float, default=0.0)
    parser.add_argument("--session-log", type=Path)
    parser.add_argument("--viewer", choices=("novnc",), default="novnc")
    parser.add_argument("--viewer-port", type=int, default=6080)
    parser.add_argument("--build-viewer", action="store_true")
    parser.add_argument("--container-name")
    parser.add_argument("--control-port", type=int, default=14540)
    parser.add_argument("--telemetry-port", type=int, default=14541)
    parser.add_argument("--sitl-lock-path", type=Path, default=default_lock_path())
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.snapshot_interval <= 0 or args.check_heartbeat < 0:
        parser.error("snapshot-interval must be positive and check-heartbeat non-negative")
    if args.operator_link and not args.onboard_task:
        # The GCS supplies the mission; the onboard run still needs a task label.
        args.onboard_task = "operator link mission"
    try:
        args.takeoff_method = resolve_takeoff_method(args.airframe, args.takeoff_method)
        validate_runtime_prerequisites(onboard=bool(args.onboard_task))
        command, plan, world = build_session_command(
            world=args.world,
            airframe=args.airframe,
            gcs_port=args.gcs_port,
            viewer=args.viewer,
            container_name=args.container_name,
            control_port=args.control_port,
            telemetry_port=args.telemetry_port,
            viewer_port=args.viewer_port,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    docker = plan.docker_executable or shutil.which("docker")
    if not docker:
        parser.error("Docker executable not found")
    ensure_viewer_image(docker, force=args.build_viewer)
    command[0] = docker
    snapshot_dir = args.snapshot_dir.resolve() if args.snapshot_dir else None
    if snapshot_dir:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        (snapshot_dir / "session-provenance.json").write_text(
            json.dumps(build_provenance(root=ROOT, world_paths=[world], command=sys.argv), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    session_log = args.session_log.resolve() if args.session_log else None
    _session_log(session_log, "plan_built", world=str(world), container=plan.container_name)
    print(f"Gazebo world: {world}")
    print(
        f"PX4 control UDP: 127.0.0.1:{args.control_port}; "
        f"telemetry: {sim_listen_host()}:{args.telemetry_port}"
    )
    if args.gcs_port:
        print(f"GCS UDP: 127.0.0.1:{args.gcs_port}")
    print(f"Browser viewer: http://127.0.0.1:{args.viewer_port}/vnc.html?autoconnect=1")
    sitl_lock = SitlLock(args.sitl_lock_path, label="sim-session", min_memory_gb=5.0)
    lock_context = sitl_lock_context(sitl_lock)
    lock_context.__enter__()
    try:
        subprocess.run(command, cwd=ROOT, check=True)
        _session_log(session_log, "container_started", container=plan.container_name)
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
        ready, spawned, logs = _wait_world_ready(docker, plan.container_name, 240.0)
        _session_log(
            session_log,
            "world_ready" if ready else "world_timeout",
            vehicle_spawned=spawned,
            log_tail=logs[-4000:],
        )
        if not ready:
            raise RuntimeError("PX4/Gazebo world did not become ready within 240 seconds")
        if spawned:
            _session_log(session_log, "vehicle_spawned")
        if snapshot_dir:
            _snapshot(docker, plan.container_name, snapshot_dir, label="world-ready")
        heartbeat_port = args.gcs_port or args.telemetry_port
        if args.check_heartbeat:
            try:
                details = _heartbeat_details(heartbeat_port, args.check_heartbeat)
            except RuntimeError as exc:
                _session_log(session_log, "heartbeat_failed", error=str(exc))
                raise
            if details is None:
                _session_log(session_log, "heartbeat_timeout", port=heartbeat_port)
                raise RuntimeError(f"no MAVLink HEARTBEAT received on UDP {heartbeat_port}")
            _session_log(session_log, "heartbeat", port=heartbeat_port, **details)
            if snapshot_dir:
                details = {**details, "provenance": build_provenance(
                    root=ROOT, config_paths=[args.onboard_out / "onboard-config.json"],
                    world_paths=[world], command=sys.argv,
                )}
                (snapshot_dir / "heartbeat.json").write_text(
                    json.dumps(details, indent=2) + "\n", encoding="utf-8"
                )
            print(f"PX4 heartbeat: received on UDP {heartbeat_port}: {details}")
        stop_snapshots = threading.Event()

        def snapshot_loop() -> None:
            while snapshot_dir and not stop_snapshots.wait(args.snapshot_interval):
                _snapshot(docker, plan.container_name, snapshot_dir)

        snapshot_thread = threading.Thread(target=snapshot_loop, daemon=True)
        snapshot_thread.start()
        if args.onboard_task:
            _session_log(session_log, "task_start", task=args.onboard_task)
            code = _run_task(
                args.onboard_task,
                world,
                args.control_port,
                args.onboard_out,
                plan.container_name,
                timeout_s=args.task_timeout_s,
                operator_link=_operator_link_config(args) if args.operator_link else None,
                airframe=args.airframe,
                takeoff_method=args.takeoff_method,
                telemetry_port=args.telemetry_port,
                onboard_defaults=args.onboard_defaults,
                artifacts_root=args.artifacts_root,
            )
            # The collector writes mission_summary.txt per episode (no report.json).
            summary = next(args.onboard_out.rglob("mission_summary.txt"), None)
            _session_log(
                session_log,
                "task_end",
                exit_code=code,
                summary_path=str(summary) if summary else None,
            )
            if snapshot_dir:
                _snapshot(docker, plan.container_name, snapshot_dir, label="task-end")
            stop_snapshots.set()
            if args.exit_after_task:
                return code
            if code:
                return code
        print("Interactive session is running. Press Ctrl+C to stop.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping owned simulator container...")
    finally:
        _session_log(session_log, "teardown", container=plan.container_name)
        subprocess.run([docker, "stop", "--timeout", "2", plan.container_name], check=False)
        subprocess.run([docker, "rm", plan.container_name], check=False)
        lock_context.__exit__(*sys.exc_info())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_session_command", "resolve_world", "main"]
