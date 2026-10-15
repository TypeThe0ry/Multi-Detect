#!/usr/bin/env python3
"""Run shared onboard autonomy on PX4 hardware or a latency-only episode replay."""

from __future__ import annotations

import argparse
import json
import math
import signal
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.mission_package import load_mission_package  # noqa: E402
from multidetect.onboard_runtime.bench import (  # noqa: E402
    LatencyProposer,
    LatencySlowRunner,
    generate_fixture,
)
from multidetect.onboard_runtime.camera import (  # noqa: E402
    CameraCalibration,
    OpenCVCameraSource,
)
from multidetect.onboard_runtime.policy import HomeRelativePolicy  # noqa: E402
from multidetect.onboard_runtime.projection import GroundProjector  # noqa: E402
from multidetect.onboard_runtime.replay import (  # noqa: E402
    ReplayCameraSource,
    ReplayTimeline,
    ReplayVehicle,
    load_episode,
)
from multidetect.onboard_runtime.runtime import OnboardRuntime, RuntimeConfig  # noqa: E402
from multidetect.onboard_runtime.vehicle import PymavlinkVehicle  # noqa: E402
from scripts.onboard_policy import DEFAULTS, OnboardPolicy  # noqa: E402


def _positive(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("value must be positive and finite")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    real = modes.add_parser("real", help="serial/UDP PX4 and V4L2/CSI camera")
    real.add_argument("--endpoint", help="/dev/ttyTHS1 or udpin:0.0.0.0:14540")
    real.add_argument("--baud", type=int, default=921600)
    real.add_argument("--camera", help="/dev/video0, device index or pipeline")
    real.add_argument("--camera-backend", choices=("v4l2", "gstreamer"), default="v4l2")
    real.add_argument("--calibration", type=Path)
    # Accepted for migration diagnostics, but _admit_package rejects this
    # legacy path for real flight; policy/task remain valid for replay only.
    real.add_argument("--policy-config", type=Path)
    real.add_argument("--task")
    real.add_argument("--mission-package", type=Path,
                      help="signed self-contained mission package admitted onboard")
    real.add_argument("--operator-key", type=Path,
                      help="operator verification key for --mission-package")
    real.add_argument("--altitude", type=_positive, default=10)
    real.add_argument("--startup-timeout", type=_positive, default=30)
    real.add_argument("--takeoff-timeout", type=_positive, default=45)
    real.add_argument("--shutdown-action", choices=("land", "rtl"), default="land")
    real.add_argument("--completion-action", choices=("land", "rtl"), default="land")
    replay = modes.add_parser("replay", help="recorded RGB/MAVLink; fake model latency only")
    source = replay.add_mutually_exclusive_group(required=True)
    source.add_argument("--episode", type=Path)
    source.add_argument("--generate-fixture", type=Path, help="explicit synthetic bench input")
    replay.add_argument("--calibration", type=Path)
    replay.add_argument("--mission-package", type=Path)
    replay.add_argument("--operator-key", type=Path)
    replay.add_argument("--slow-latency", type=float, default=1.5)
    replay.add_argument("--detector-latency", type=float, default=0.07)
    for mode in (real, replay):
        mode.add_argument("--stream-hz", type=_positive, default=20)
        mode.add_argument("--camera-timeout", type=_positive, default=0.75)
        mode.add_argument("--pose-timeout", type=_positive, default=0.75)
        mode.add_argument("--heartbeat-timeout", type=_positive, default=3)
        mode.add_argument("--hold-before-failsafe", type=_positive, default=3)
        mode.add_argument("--autonomy-timeout", type=_positive, default=15)
        mode.add_argument("--failsafe", choices=("hold", "land", "rtl"),
                          required=mode is real, default=None if mode is real else "hold")
        mode.add_argument("--duration", type=_positive, default=None if mode is real else 6)
        mode.add_argument("--work-dir", type=Path, default=ROOT / "tmp/onboard-runtime/frames")
        mode.add_argument("--output", type=Path)
    return parser


def _admit_package(args: argparse.Namespace) -> tuple[Any, dict[str, Any]]:
    """Admit a package before opening a camera, vehicle, or model backend."""
    package_path = getattr(args, "mission_package", None)
    if package_path is None:
        if getattr(args, "mode", None) == "real":
            raise ValueError(
                "real runtime requires a signed --mission-package and --operator-key"
            )
        return None, {}
    # Admission and execution must refer to the exact same files.  Resolve the
    # paths once here, before any source/model backend is opened, and retain the
    # resolved values on the namespace for callers that construct their own
    # execution path.
    package_path = Path(package_path).expanduser().resolve()
    args.mission_package = package_path
    if not package_path.is_file():
        raise ValueError(f"mission package not found: {package_path}")
    key_path = getattr(args, "operator_key", None)
    if key_path is None:
        raise ValueError("--operator-key is required for a signed mission package")
    key_path = Path(key_path).expanduser().resolve()
    args.operator_key = key_path
    if not key_path.is_file():
        raise ValueError(f"operator key not found: {key_path}")
    try:
        package = load_mission_package(
            package_path, operator_key_path=key_path, require_signature=True
        )
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f"mission package admission refused: {exc}") from exc
    document = package.document
    task = document.get("task_text") or (package.plan.goal if package.plan else None)
    if not task:
        raise ValueError("mission package has no task_text or plan goal")
    # Package values are authoritative. A policy file can only provide local model
    # and runtime tuning overrides; MissionSupervisor retains the uploaded policy.
    return package, {
        "mission_package": str(package_path),
        "operator_key_path": str(key_path),
        "task_text": task,
        "mission": "package" if package.plan else "from_text",
        "refs": package.refs,
    }


def _ready(vehicle, camera, config: RuntimeConfig, timeout_s: float, *, require_ground=False):
    until = time.monotonic() + timeout_s
    last_fault = "no telemetry"
    while time.monotonic() < until:
        state, frame = vehicle.snapshot(), camera.latest()
        for error in (getattr(camera, "error", None),):
            if error is not None:
                raise RuntimeError(f"camera startup failed: {error}")
        now = time.monotonic()
        ground_ready = True
        if require_ground:
            if state.armed is True or state.landed is False:
                raise RuntimeError("real runtime must start on the ground, disarmed")
            ground_ready = (
                state.home_ned is not None and state.armed is False and state.landed is True
                and state.landed_monotonic is not None
                and 0 <= now - state.landed_monotonic <= config.heartbeat_timeout_s
            )
        checks = (
            (state.pose_monotonic, config.pose_timeout_s),
            (state.attitude_monotonic, config.pose_timeout_s),
            (state.heartbeat_monotonic, config.heartbeat_timeout_s),
            (frame.timestamp_monotonic if frame else None, config.camera_timeout_s),
        )
        if ground_ready and all(
            stamp is not None and 0 <= now - stamp <= limit for stamp, limit in checks
        ):
            return state, frame
        last_fault = (
            "waiting for fresh position, attitude, heartbeat, camera and required home/landed"
        )
        time.sleep(0.01)
    raise TimeoutError(f"startup timeout: {last_fault}")


def _validate_local_models(config: dict[str, Any]) -> None:
    for key in ("yoloe_weights", "gguf", "mmproj", "llama_server"):
        value = Path(config.get(key, DEFAULTS[key]))
        path = value if value.is_absolute() else ROOT / value
        if not path.is_file():
            raise FileNotFoundError(f"local {key} is missing: {path}; automatic download disabled")


def run(args: argparse.Namespace) -> dict[str, Any]:
    package, package_options = _admit_package(args)
    if args.mode == "real":
        missing = [name for name in ("endpoint", "camera", "calibration")
                   if getattr(args, name, None) is None]
        if missing:
            raise ValueError("real runtime requires " + ", ".join(f"--{name}" for name in missing))
    config = RuntimeConfig(
        stream_hz=args.stream_hz, camera_timeout_s=args.camera_timeout,
        pose_timeout_s=args.pose_timeout, heartbeat_timeout_s=args.heartbeat_timeout,
        hold_before_failsafe_s=args.hold_before_failsafe,
        autonomy_timeout_s=args.autonomy_timeout, failsafe=args.failsafe,
        shutdown_action=getattr(args, "shutdown_action", "land"),
        completion_action=getattr(args, "completion_action", "land"),
    )
    calibration = CameraCalibration.load(args.calibration) if args.calibration else None
    proposer = slow = None
    if args.mode == "replay":
        path = (generate_fixture(args.generate_fixture, duration_s=args.duration + 1)
                if args.generate_fixture else args.episode)
        recording = load_episode(path, calibration)
        timeline = ReplayTimeline()
        camera = ReplayCameraSource(recording, timeline)
        vehicle = ReplayVehicle(recording, timeline)
        proposer = LatencyProposer(args.detector_latency)
        slow = LatencySlowRunner(args.slow_latency)
        task = ((package_options.get("task_text") if package else None)
                or recording.task_text or "Find the object and record its position.")
        options: dict[str, Any] = {"mission": "find", "memory_enabled": False,
                                   "confirm_min_side_px": 1000000}
    else:
        options = (json.loads(args.policy_config.read_text(encoding="utf-8"))
                   if getattr(args, "policy_config", None) else {})
        options.setdefault("mission", "find")
        _validate_local_models(options)
        # The shared controller's image bearing/rotation assumes a forward camera.
        # Calibrated ground rays support all axes, but flight control does not yet.
        for key in ("mount_yaw_right_deg", "mount_roll_clockwise_deg"):
            if abs(float(calibration.extrinsics.get(key, 0))) > 1e-6:
                raise ValueError(f"shared flight controller currently requires {key}=0")
        camera_source = int(args.camera) if args.camera.isdecimal() else args.camera
        camera = OpenCVCameraSource(camera_source, calibration, backend=args.camera_backend)
        vehicle = PymavlinkVehicle(args.endpoint, baud=args.baud)
        task = package_options.get("task_text") if package else getattr(args, "task", None)
    if package:
        options.update(package_options)
        area = package.document["operating_area"]
        altitude = getattr(args, "altitude", None)
        if altitude is not None and not (
            area["min_altitude_m"] <= altitude <= area["max_altitude_m"]
        ):
            raise ValueError("launch altitude outside mission package limits")
    runtime = None
    policy = None
    previous_signals = {}
    report: dict[str, Any] = {"mode": args.mode}
    failure: str | None = None
    try:
        vehicle.start()
        camera.start()
        state, frame = _ready(
            vehicle, camera, config, getattr(args, "startup_timeout", 3),
            require_ground=args.mode == "real",
        )
        if args.mode == "real" and state.home_ned is None:
            raise RuntimeError("HOME_POSITION in local NED is required before mission startup")
        if args.mode == "real" and (state.armed is not False or state.landed is not True):
            raise RuntimeError("real runtime must start on the ground, disarmed")
        home = state.home_ned or (state.position_ned[0], state.position_ned[1], 0)
        calibrated = frame.calibration
        projector = GroundProjector(calibrated) if args.mode == "real" else None
        if args.mode == "real":
            options["camera_pitch_down_rad"] = math.radians(
                calibrated.extrinsics["mount_pitch_down_deg"]
            )
        raw_policy = OnboardPolicy(
            task_text=task, world_path=None,
            altitude_m=getattr(args, "altitude", max(1, home[2] - state.position_ned[2])),
            camera_info=calibrated.camera_info, image_size=(calibrated.width, calibrated.height),
            work_dir=args.work_dir, config=options, proposer=proposer, slow_runner=slow,
            ground_projector=projector, home_ned=(home[0], home[1], 0),
            mission_package=package,
        )
        policy = HomeRelativePolicy(raw_policy, home[2], proposer)
        # Model startup may take minutes. Recheck input freshness before streaming.
        ready_state, _frame = _ready(
            vehicle, camera, config, getattr(args, "startup_timeout", 3),
            require_ground=args.mode == "real",
        )
        if args.mode == "real" and (ready_state.armed is not False
                                    or ready_state.landed is not True):
            raise RuntimeError("vehicle must remain disarmed and landed during model startup")
        if args.mode == "real" and ready_state.home_ned != home:
            raise RuntimeError(
                "PX4 home changed during model startup; restart with stable telemetry"
            )
        runtime = OnboardRuntime(
            vehicle, camera, policy, config=config, home_ned=home if args.mode == "real" else None,
        )
        takeover = args.mode == "replay"
        runtime.start(active=takeover, own_flight=args.mode == "real")
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_signals[sig] = signal.getsignal(sig)
            signal.signal(sig, lambda _sig, _frame: runtime.request_stop())
        if not takeover:
            try:
                runtime.arm_and_takeoff(args.altitude, timeout_s=args.takeoff_timeout)
            except Exception as exc:
                if runtime.owns_flight and not runtime.stop_requested:
                    runtime.fail(f"takeoff_error: {exc}")
                else:
                    raise
        runtime.run(args.duration)
    except (Exception, KeyboardInterrupt) as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            if runtime:
                runtime.close()
            else:
                cleanup_errors = []
                for close in (policy.close if policy else None, camera.close, vehicle.close):
                    if close:
                        try:
                            close()
                        except Exception as exc:
                            cleanup_errors.append(str(exc))
                if cleanup_errors:
                    raise RuntimeError("; ".join(cleanup_errors))
        except Exception as exc:
            failure = f"{failure + '; ' if failure else ''}shutdown: {exc}"
        for sig, handler in previous_signals.items():
            signal.signal(sig, handler)
    if runtime:
        report.update(runtime.report())
    if args.mode == "replay":
        report.update({
            "episode": str(path), "synthetic": recording.synthetic,
            "replay_speed": 1.0, "detector_latency_s": proposer.latency_s,
            "slow_latency_s": slow.latency_s, "detector_calls": proposer.calls,
            "slow_calls": slow.calls, "transport_timing": vehicle.timing_report(),
            "recording_duration_s": recording.duration_s,
            "outbound_commands_transmitted": False,
        })
        timing = vehicle.timing_report()
        report["timing_passed"] = (
            timing["setpoint_count"] > 1 and timing["setpoint_rate_hz"] >= 10
            and timing["setpoint_max_gap_s"] < 0.5
        )
        report["slow_call_exercised"] = slow.calls > 0
    if failure:
        report["error"] = failure
    report["ok"] = failure is None and (runtime is None or runtime.hazard is None)
    if args.mode == "replay":
        report["ok"] = report["ok"] and report["timing_passed"]
    return report


def _has_option(argv: list[str], name: str) -> bool:
    """Return whether *name* is present as a flag or ``--name=value``."""
    return any(token == name or token.startswith(name + "=") for token in argv)


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    # A package-only invocation is the explicit comms-free real entry. Legacy
    # callers that name a mode continue to use the subcommand grammar.
    if argv and argv[0] not in {"real", "replay", "-h", "--help"}:
        argv = ["real", *argv]
        if _has_option(argv, "--mission-package") and not _has_option(argv, "--failsafe"):
            argv.extend(["--failsafe", "land"])
    args = build_parser().parse_args(argv)
    try:
        report = run(args)
    except Exception as exc:
        report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    result = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result + "\n", encoding="utf-8")
    print(result)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
