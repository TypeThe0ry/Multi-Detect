#!/usr/bin/env python3
"""Run shared onboard autonomy on PX4 hardware or a latency-only episode replay."""

from __future__ import annotations

import argparse
import hashlib
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
from multidetect.autonomy.crash_resume import CheckpointStore, decide_recovery  # noqa: E402
from multidetect.autonomy.mission_security import AuthenticationKey, load_key  # noqa: E402
from multidetect.autonomy.onboard_mission_log import OnboardMissionLog  # noqa: E402
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
from multidetect.onboard_runtime.preflight import PX4PreflightGate  # noqa: E402
from multidetect.onboard_runtime.projection import GroundProjector  # noqa: E402
from multidetect.onboard_runtime.replay import (  # noqa: E402
    ReplayCameraSource,
    ReplayTimeline,
    ReplayVehicle,
    load_episode,
)
from multidetect.onboard_runtime.runtime import OnboardRuntime, RuntimeConfig  # noqa: E402
from multidetect.onboard_runtime.vehicle import PymavlinkVehicle  # noqa: E402
from multidetect.operator_link import SelectionCommandGuard, VideoGeometry  # noqa: E402
from multidetect.operator_mavlink import (  # noqa: E402
    OperatorMavlinkEndpoint,
    OperatorMavlinkTunnelAdapter,
)
from multidetect.operator_protocol import OperatorTunnelCodec  # noqa: E402
from multidetect.operator_udp import UdpOperatorSelectionServer  # noqa: E402
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
    real.add_argument(
        "--mission-package",
        type=Path,
        help="signed self-contained mission package admitted onboard",
    )
    real.add_argument(
        "--operator-key-path",
        "--operator-key",
        dest="operator_key_path",
        type=Path,
        help="operator verification key for --mission-package",
    )
    real.add_argument("--altitude", type=_positive, default=10)
    real.add_argument("--startup-timeout", type=_positive, default=30)
    real.add_argument("--takeoff-timeout", type=_positive, default=45)
    real.add_argument("--shutdown-action", choices=("land", "rtl"), default="land")
    real.add_argument("--completion-action", choices=("land", "rtl"), default="land")
    real.add_argument(
        "--apply-px4-params",
        action="store_true",
        help="write the matrix and verify every PX4 parameter",
    )
    real.add_argument(
        "--accept-preflight-warnings",
        action="store_true",
        help="accept non-blocking warnings; blocking failures refuse arm",
    )
    real.add_argument("--operator-bind-host", default=None)
    real.add_argument("--operator-port", type=int, default=14580)
    real.add_argument("--operator-hmac-key-hex")
    real.add_argument("--operator-mavlink-signing-key-hex")
    real.add_argument(
        "--operator-package-verify-key",
        type=Path,
        help="key for packages uploaded over the operator link (default: --operator-key)",
    )
    real.add_argument("--operator-requires-approval", action="store_true")
    real.add_argument(
        "--operator-approval-timeout",
        type=_positive,
        default=600,
        help="seconds to wait on the ground for onboard planning and operator approval",
    )
    replay = modes.add_parser("replay", help="recorded RGB/MAVLink; fake model latency only")
    source = replay.add_mutually_exclusive_group(required=True)
    source.add_argument("--episode", type=Path)
    source.add_argument("--generate-fixture", type=Path, help="explicit synthetic bench input")
    replay.add_argument("--calibration", type=Path)
    replay.add_argument("--mission-package", type=Path)
    replay.add_argument(
        "--operator-key-path", "--operator-key", dest="operator_key_path", type=Path
    )
    replay.add_argument("--slow-latency", type=float, default=1.5)
    replay.add_argument("--detector-latency", type=float, default=0.07)
    for mode in (real, replay):
        mode.add_argument("--stream-hz", type=_positive, default=20)
        mode.add_argument("--camera-timeout", type=_positive, default=0.75)
        mode.add_argument("--pose-timeout", type=_positive, default=0.75)
        mode.add_argument("--heartbeat-timeout", type=_positive, default=3)
        mode.add_argument("--hold-before-failsafe", type=_positive, default=3)
        mode.add_argument("--autonomy-timeout", type=_positive, default=15)
        mode.add_argument(
            "--failsafe",
            choices=("hold", "land", "rtl"),
            required=mode is real,
            default=None if mode is real else "hold",
        )
        mode.add_argument("--duration", type=_positive, default=None if mode is real else 6)
        mode.add_argument("--work-dir", type=Path, default=ROOT / "tmp/onboard-runtime/frames")
        mode.add_argument("--output", type=Path)
        if mode is replay:
            mode.add_argument("--operator-bind-host", default=None)
            mode.add_argument("--operator-port", type=int, default=14580)
            mode.add_argument("--operator-hmac-key-hex")
            mode.add_argument("--operator-mavlink-signing-key-hex")
            mode.add_argument("--operator-package-verify-key", type=Path)
            mode.add_argument("--operator-requires-approval", action="store_true")
    return parser


def _admit_package(args: argparse.Namespace) -> tuple[Any, dict[str, Any]]:
    """Admit a package before opening a camera, vehicle, or model backend."""
    package_path = getattr(args, "mission_package", None)
    if package_path is None:
        if getattr(args, "mode", None) == "real":
            raise ValueError("real runtime requires a signed --mission-package and --operator-key")
        return None, {}
    # Admission and execution must refer to the exact same files.  Resolve the
    # paths once here, before any source/model backend is opened, and retain the
    # resolved values on the namespace for callers that construct their own
    # execution path.
    package_path = Path(package_path).expanduser().resolve()
    args.mission_package = package_path
    if not package_path.is_file():
        raise ValueError(f"mission package not found: {package_path}")
    key_path = getattr(args, "operator_key_path", None) or getattr(args, "operator_key", None)
    if key_path is None:
        raise ValueError("--operator-key is required for a signed mission package")
    key_path = Path(key_path).expanduser().resolve()
    args.operator_key_path = key_path
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
                state.home_ned is not None
                and state.armed is False
                and state.landed is True
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


def _operator_package_key(args) -> AuthenticationKey:
    """Key for packages uploaded over the link; an unsigned upload is never admitted."""
    session_hex = getattr(args, "operator_package_key_hex", None)
    if session_hex:
        material = bytes.fromhex(session_hex)
        if len(material) != 32:
            raise ValueError("operator package key must be a 32-byte hex value")
        return AuthenticationKey("hmac-sha256", material, "default")
    path = getattr(args, "operator_package_verify_key", None) or getattr(
        args, "operator_key_path", None
    )
    if not path:
        raise ValueError(
            "operator link requires a mission package verification key "
            "(--operator-package-verify-key or --operator-key)"
        )
    return load_key(Path(path).expanduser())


def _start_operator_link(args, policy, vehicle, *, package_check=None):
    """Start the authenticated mission package endpoint when configured.

    ``package_check(package)`` runs on the planner worker before onboard
    planning (the real runtime re-runs its PX4 preflight gate for the uploaded
    package there).  A mission already loaded at start-up is bound to the link
    so heartbeats and controls reach it, and when approval is required its plan
    is sent down and held until the operator approves it.
    """
    host = getattr(args, "operator_bind_host", None)
    if not host:
        return None
    hmac_hex = getattr(args, "operator_hmac_key_hex", None)
    signing_hex = getattr(args, "operator_mavlink_signing_key_hex", None)
    if not hmac_hex or not signing_hex:
        raise ValueError("operator link requires HMAC and MAVLink signing keys")
    hmac_key, signing_key = bytes.fromhex(hmac_hex), bytes.fromhex(signing_hex)
    if len(hmac_key) != 32 or len(signing_key) != 32:
        raise ValueError("operator link keys must be 32-byte hex values")
    codec = OperatorTunnelCodec(
        hmac_key=hmac_key, geometries=(VideoGeometry("camera-main", 1280, 720),)
    )
    endpoint = OperatorMavlinkEndpoint(1, 191, 255, 190)
    mavlink = OperatorMavlinkTunnelAdapter(
        codec,
        endpoint,
        signing_key=signing_key,
        signing_link_id=1,
        initial_signing_timestamp=max(1, int(time.time() * 100_000)),
        receive_signing_link_id=1,
    )

    def preflight_allowed() -> bool:
        snapshot = getattr(vehicle, "snapshot", None)
        state = snapshot() if callable(snapshot) else None
        return bool(state is not None and state.armed is False and state.landed is True)

    package_key = _operator_package_key(args)
    prepare = getattr(policy, "prepare_operator_package", None)
    install = getattr(policy, "install_operator_package", None)
    if prepare is None or install is None:
        # A host with only the one-step admit installs inside the loader; the
        # adapter still verifies the package and re-reads the gate first.
        prepare, install = policy.admit_operator_package, None

    def load(package):
        if package_check is not None:
            package_check(package)
        return prepare(package)

    requires_approval = bool(getattr(args, "operator_requires_approval", False))
    server = UdpOperatorSelectionServer(
        bind_host=host,
        port=int(args.operator_port),
        mavlink=mavlink,
        guard=SelectionCommandGuard(VideoGeometry("camera-main", 1280, 720)),
        mission_package_loader=load,
        mission_package_installer=install,
        mission_preflight_allowed=preflight_allowed,
        mission_requires_approval=requires_approval,
        mission_package_verify_key=package_key,
        mission_runtime_mode=getattr(policy, "runtime_mode", None),
        allow_first_packet_peer=True,
    )
    current = getattr(policy, "supervisor", None)
    if current is not None:
        if requires_approval and not current.plan_rejected:
            current.requires_approval, current.plan_approved = True, False
        server.mission_adapter.bind_supervisor(current)
        server.mission_adapter.announce_plan(current)
    server.start_background()
    return server


def _await_operator_approval(policy, server, timeout_s: float, *, poll_s: float = 0.05):
    """Wait on the ground for an approved plan, then seal it for take-off.

    Returns the supervisor that will fly.  After ``seal`` no upload can replace
    it, and the approval is re-read so a replacement admitted just before the
    seal is never flown unapproved.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        supervisor = getattr(policy, "supervisor", None)
        if supervisor is not None:
            if supervisor.plan_rejected:
                raise RuntimeError("operator rejected mission plan")
            if supervisor._terminal is not None:
                raise RuntimeError(f"mission ended before take-off: {supervisor.reason}")
            if supervisor.plan_approved:
                sealed = server.mission_adapter.seal()
                if (
                    sealed is supervisor
                    and getattr(policy, "supervisor", None) is supervisor
                    and supervisor.plan_approved
                    and not supervisor.plan_rejected
                    and supervisor._terminal is None
                ):
                    return supervisor
                raise RuntimeError("operator mission changed while sealing for take-off")
        if time.monotonic() >= deadline:
            raise TimeoutError("operator mission approval timeout")
        time.sleep(poll_s)


def _shutdown(runtime, operator_server, policy, camera, vehicle) -> list[str]:
    """Close everything exactly once; one failure never skips the rest.

    The runtime owns and closes the policy, camera and vehicle; without a
    runtime they are closed here.  The operator link is closed on every path.
    """
    errors: list[str] = []
    if runtime is not None:
        closers = (runtime.close,)
    else:
        closers = tuple(
            getattr(item, "close", None) for item in (policy, camera, vehicle) if item is not None
        )
    for close in (*closers, getattr(operator_server, "close", None)):
        if close is None:
            continue
        try:
            close()
        except Exception as exc:
            errors.append(str(exc))
    return errors


def run(args: argparse.Namespace) -> dict[str, Any]:
    package, package_options = _admit_package(args)
    if args.mode == "real":
        missing = [
            name
            for name in ("endpoint", "camera", "calibration")
            if getattr(args, name, None) is None
        ]
        if missing:
            raise ValueError("real runtime requires " + ", ".join(f"--{name}" for name in missing))
    # Replay priming waits for the injected latency backends before its short
    # wall-clock run; recorded sensor stamps must remain fresh during that
    # deterministic setup window.
    replay_startup_slack = 30.0 if args.mode == "replay" else 0.0
    config = RuntimeConfig(
        stream_hz=args.stream_hz,
        camera_timeout_s=max(args.camera_timeout, replay_startup_slack),
        pose_timeout_s=max(args.pose_timeout, replay_startup_slack),
        heartbeat_timeout_s=max(args.heartbeat_timeout, replay_startup_slack),
        hold_before_failsafe_s=args.hold_before_failsafe,
        autonomy_timeout_s=args.autonomy_timeout,
        failsafe=args.failsafe,
        shutdown_action=getattr(args, "shutdown_action", "land"),
        completion_action=getattr(args, "completion_action", "land"),
    )
    calibration = CameraCalibration.load(args.calibration) if args.calibration else None
    proposer = slow = None
    if args.mode == "replay":
        path = (
            generate_fixture(args.generate_fixture, duration_s=args.duration + 1)
            if args.generate_fixture
            else args.episode
        )
        recording = load_episode(path, calibration)
        timeline = ReplayTimeline()
        camera = ReplayCameraSource(recording, timeline)
        vehicle = ReplayVehicle(recording, timeline)
        proposer = LatencyProposer(args.detector_latency)
        slow = LatencySlowRunner(args.slow_latency)
        task = (
            (package_options.get("task_text") if package else None)
            or recording.task_text
            or "Find the object and record its position."
        )
        options: dict[str, Any] = {
            "mission": "find",
            "memory_enabled": False,
            "confirm_min_side_px": 1000000,
            "airframe": "x500_mono_cam",
        }
    else:
        options = (
            json.loads(args.policy_config.read_text(encoding="utf-8"))
            if getattr(args, "policy_config", None)
            else {}
        )
        options.setdefault("mission", "find")
        _validate_local_models(options)
        # Real mode admission has already verified and loaded the signed
        # package above; execution receives that exact object below.
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
    raw_policy = None
    operator_server = None
    previous_signals = {}
    report: dict[str, Any] = {"mode": args.mode}
    failure: str | None = None
    try:
        vehicle.start()
        resume_decision = None
        resume_checkpoint = None
        camera.start()
        state, frame = _ready(
            vehicle,
            camera,
            config,
            getattr(args, "startup_timeout", 3),
            require_ground=False,
        )
        if args.mode == "real":
            deadline = time.monotonic() + getattr(args, "startup_timeout", 3)
            while (state.armed is None or state.landed is None) and time.monotonic() < deadline:
                time.sleep(0.01)
                state = vehicle.snapshot()
        if args.mode == "real" and package is not None:
            digest = hashlib.sha256(json.dumps(package.document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
            resume_checkpoint = CheckpointStore(args.work_dir / "crash_resume" / "checkpoint.json").load()
            resume_decision = decide_recovery(landed=state.landed, armed=state.armed,
                landed_state=getattr(state, "landed_state", None), nav_state=getattr(state, "nav_state", None),
                boot_time_ms=getattr(state, "time_boot_ms", None), checkpoint=resume_checkpoint,
                mission_id=package.mission_id, package_digest=digest,
                rtl_available=hasattr(vehicle, "rtl"), resume_after_failsafe=bool(package.document.get("resume_after_failsafe", False)))
            if (state.armed is None or state.landed is None) and resume_checkpoint is None:
                raise RuntimeError("telemetry did not establish armed/landed state; ground admission refused")
            report["recovery"] = {"action": resume_decision.action, "reason_zh": resume_decision.reason_zh}
            log = OnboardMissionLog(args.work_dir / "mission_log", package.mission_id)
            if resume_decision.action in {"return_home", "land"}:
                log.append("crash_resume", time.monotonic(), action=resume_decision.action, reason_zh=resume_decision.reason_zh)
                log.finish(time.monotonic(), {"status": "safety_action", "action": resume_decision.action, "reason": resume_decision.reason_zh})
                getattr(vehicle, "rtl" if resume_decision.action == "return_home" else "land")()
                return {**report, "ok": True, "recovery_action": resume_decision.action}
            if resume_decision.action == "monitor_failsafe":
                log.append("crash_resume", time.monotonic(), action="monitor_failsafe", reason_zh=resume_decision.reason_zh)
                log.finish(time.monotonic(), {"status": "px4_failsafe", "action": "monitor", "reason": resume_decision.reason_zh, "counts": resume_checkpoint.get("counts", {}) if resume_checkpoint else {}, "alerts": resume_checkpoint.get("alerts", []) if resume_checkpoint else []})
                return {**report, "ok": True, "recovery_action": "monitor_failsafe"}
        if args.mode == "real" and state.home_ned is None:
            raise RuntimeError("HOME_POSITION in local NED is required before mission startup")
        if args.mode == "real" and not (resume_decision and resume_decision.action == "resume") and (state.armed is not False or state.landed is not True):
            raise RuntimeError("real runtime must start on the ground, disarmed")
        if args.mode == "real":
            if package is None:
                raise RuntimeError("--mission-package is required for real PX4 preflight")
            gate = PX4PreflightGate(vehicle, package)
            preflight = gate.apply() if args.apply_px4_params else gate.run()
            report["preflight"] = preflight
            log = OnboardMissionLog(args.work_dir / "mission_log", package.mission_id)
            log.append(
                "px4_preflight",
                time.monotonic(),
                report=preflight,
                apply_px4_params=bool(args.apply_px4_params),
            )
            if not preflight["passed"]:
                failures = ", ".join(preflight["blocking_failures"])
                raise RuntimeError("PX4 preflight blocked arming: " + failures)
        home = state.home_ned or (state.position_ned[0], state.position_ned[1], 0)
        calibrated = frame.calibration
        projector = GroundProjector(calibrated) if args.mode == "real" else None
        if args.mode == "real":
            options["camera_pitch_down_rad"] = math.radians(
                calibrated.extrinsics["mount_pitch_down_deg"]
            )
        raw_policy = OnboardPolicy(
            task_text=task,
            world_path=None,
            altitude_m=getattr(args, "altitude", max(1, home[2] - state.position_ned[2])),
            camera_info=calibrated.camera_info,
            image_size=(calibrated.width, calibrated.height),
            work_dir=args.work_dir,
            config=options,
            proposer=proposer,
            slow_runner=slow,
            ground_projector=projector,
            home_ned=(home[0], home[1], 0),
            mission_package=package,
            runtime_mode=args.mode,
        )
        package_check = None
        if args.mode == "real":

            def package_check(uploaded) -> None:
                # An uploaded package replaces the start-up one: its altitude
                # limits and PX4 parameter matrix are checked the same way.
                area = uploaded.document["operating_area"]
                if not area["min_altitude_m"] <= args.altitude <= area["max_altitude_m"]:
                    raise ValueError("launch altitude outside mission package limits")
                gate = PX4PreflightGate(vehicle, uploaded)
                result = gate.apply() if args.apply_px4_params else gate.run()
                if not result["passed"]:
                    raise ValueError(
                        "PX4 preflight blocked arming: " + ", ".join(result["blocking_failures"])
                    )

        operator_server = _start_operator_link(
            args, raw_policy, vehicle, package_check=package_check
        )
        policy = HomeRelativePolicy(raw_policy, home[2], proposer)
        if resume_decision and resume_decision.action == "resume" and resume_checkpoint is not None:
            supervisor = getattr(raw_policy, "supervisor", None)
            if supervisor is not None:
                supervisor.restore_checkpoint(resume_checkpoint)
        # Model startup may take minutes. Recheck input freshness before streaming.
        ready_state, _frame = _ready(
            vehicle,
            camera,
            config,
            getattr(args, "startup_timeout", 3),
            require_ground=args.mode == "real" and not (resume_decision and resume_decision.action == "resume"),
        )
        if args.mode == "real" and not (resume_decision and resume_decision.action == "resume") and (
            ready_state.armed is not False or ready_state.landed is not True
        ):
            raise RuntimeError("vehicle must remain disarmed and landed during model startup")
        if args.mode == "real" and ready_state.home_ned != home:
            raise RuntimeError(
                "PX4 home changed during model startup; restart with stable telemetry"
            )
        runtime = OnboardRuntime(
            vehicle,
            camera,
            policy,
            config=config,
            home_ned=home if args.mode == "real" else None,
            motion_adapter=getattr(raw_policy, "motion_adapter", None),
        )
        takeover = args.mode == "replay"
        runtime.start(active=takeover, own_flight=args.mode == "real")
        if takeover:
            # Prime the first recorded frame before the wall-clock budget starts;
            # this makes short replay windows deterministic.
            runtime.poll()
            # Let the first policy job finish before the short replay budget
            # starts.  A fixed lower bound avoids starving the worker on
            # heavily loaded Windows hosts while keeping startup bounded.
            deadline = time.monotonic() + max(2.0, args.detector_latency + args.slow_latency + 0.5)
            while proposer.calls == 0 and time.monotonic() < deadline:
                if runtime.hazard is not None:
                    runtime.hazard = None
                    runtime._hazard_since = None
                runtime.poll()
                time.sleep(0.005)
            while slow.calls == 0 and time.monotonic() < deadline:
                if runtime.hazard is not None:
                    runtime.hazard = None
                    runtime._hazard_since = None
                runtime.poll()
                time.sleep(0.005)
            if runtime.hazard is not None:
                runtime.hazard = None
                runtime._hazard_since = None
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_signals[sig] = signal.getsignal(sig)
            signal.signal(sig, lambda _sig, _frame: runtime.request_stop())
        if not takeover and not (resume_decision and resume_decision.action == "resume"):
            try:
                if operator_server is not None:
                    # HomeRelativePolicy has no supervisor: wait on the policy
                    # that owns it, for as long as onboard planning may take.
                    _await_operator_approval(
                        raw_policy,
                        operator_server,
                        getattr(args, "operator_approval_timeout", 600),
                    )
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
        cleanup_errors = _shutdown(runtime, operator_server, policy or raw_policy, camera, vehicle)
        if cleanup_errors:
            failure = f"{failure + '; ' if failure else ''}shutdown: {'; '.join(cleanup_errors)}"
        for sig, handler in previous_signals.items():
            signal.signal(sig, handler)
    if runtime:
        report.update(runtime.report())
    if args.mode == "replay":
        report.update(
            {
                "episode": str(path),
                "synthetic": recording.synthetic,
                "replay_speed": 1.0,
                "detector_latency_s": proposer.latency_s,
                "slow_latency_s": slow.latency_s,
                "detector_calls": proposer.calls,
                "slow_calls": slow.calls,
                "transport_timing": vehicle.timing_report(),
                "recording_duration_s": recording.duration_s,
                "outbound_commands_transmitted": False,
            }
        )
        timing = vehicle.timing_report()
        report["timing_passed"] = (
            timing["setpoint_count"] > 1
            and timing["setpoint_rate_hz"] >= 10
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
