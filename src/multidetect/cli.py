from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import MissionConfig
from .live import LiveMissionRunner, LiveRunConfig
from .mission import MissionController
from .pixhawk import PixhawkReadOnlyConfig, PixhawkReadOnlyTelemetryProvider
from .replay import load_jsonl_replay
from .telemetry import FailClosedTelemetryProvider
from .vision import CaptureConfig, DetectorEnsemble, OnnxNx6Config, OnnxNx6Detector, OpenCVFrameSource


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="multi-detect",
        description="Safety-first non-hazardous mission orchestration and live perception.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-config", help="validate a mission JSON file")
    validate.add_argument("config", type=Path)

    replay = subparsers.add_parser("replay", help="run detections through the simulated loop")
    replay.add_argument("config", type=Path)
    replay.add_argument("frames", type=Path)
    replay.add_argument(
        "--simulate-authorized-cycle",
        action="store_true",
        help=(
            "explicitly act as a demo operator and complete one FakePayloadPort transaction; "
            "never controls hardware"
        ),
    )
    replay.add_argument("--operator-id", default="demo-operator")
    replay.add_argument("--audit-out", type=Path)

    camera_check = subparsers.add_parser(
        "camera-check", help="open a local/RTSP source, read one frame, and discard it"
    )
    _add_capture_arguments(camera_check)

    live = subparsers.add_parser(
        "live-camera",
        help="local/RTSP capture -> ONNX Nx6 -> safety/authorization UI; no physical release",
    )
    live.add_argument("config", type=Path)
    _add_capture_arguments(live)
    live.add_argument("--onnx-model", type=Path, required=True)
    live.add_argument("--class-names", default="fire,smoke")
    live.add_argument("--safety-onnx-model", type=Path)
    live.add_argument("--safety-class-names", default="person,firefighter")
    live.add_argument("--input-width", type=int, default=640)
    live.add_argument("--input-height", type=int, default=640)
    live.add_argument("--confidence-threshold", type=float, default=0.25)
    live.add_argument("--provider", action="append", default=[])
    live.add_argument("--pixhawk-endpoint")
    live.add_argument("--pixhawk-baud", type=int, default=57_600)
    live.add_argument("--operator-id", default="local-operator")
    live.add_argument("--max-frames", type=int)
    live.add_argument("--no-display", action="store_true")
    live.add_argument("--audit-out", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-config":
            return _validate_config(args.config)
        if args.command == "replay":
            return _run_replay(
                config_path=args.config,
                replay_path=args.frames,
                simulate_authorized_cycle=args.simulate_authorized_cycle,
                operator_id=args.operator_id,
                audit_out=args.audit_out,
            )
        if args.command == "camera-check":
            return _camera_check(_capture_config_from_args(args))
        if args.command == "live-camera":
            return _run_live_camera(args)
    except (OSError, ValueError, RuntimeError) as exc:
        _emit(
            {
                "event": "error",
                "error_type": type(exc).__name__,
                "message": str(exc),
                "simulation_only": args.command != "live-camera",
                "hardware_control_enabled": False,
            },
            stream=sys.stderr,
        )
        return 1
    parser.error(f"unsupported command: {args.command}")
    return 2


def _validate_config(path: Path) -> int:
    config = MissionConfig.from_json(path)
    _emit(
        {
            "event": "config_valid",
            "mission_id": config.mission_id,
            "mission_type": config.mission_type.value,
            "platform_mode": config.platform_mode.value,
            "payload_count": len(config.payloads),
            "human_authorization_required": config.human_authorization_required,
            "simulation_only": True,
        }
    )
    return 0


def _add_capture_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", default="0", help="camera index such as 0, or an rtsp:// URI")
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--fps", type=float)
    parser.add_argument("--rtsp-transport", choices=("tcp", "udp"), default="tcp")


def _capture_config_from_args(args: argparse.Namespace) -> CaptureConfig:
    raw_source = str(args.source).strip()
    if not raw_source:
        raise ValueError("camera source cannot be empty")
    source: int | str = int(raw_source) if raw_source.isdigit() else raw_source
    return CaptureConfig(
        source=source,
        width=args.width,
        height=args.height,
        fps=args.fps,
        rtsp_transport=args.rtsp_transport,
    )


def _parse_class_names(raw: str) -> tuple[str, ...]:
    labels = tuple(label.strip() for label in raw.split(",") if label.strip())
    if not labels:
        raise ValueError("class names must contain at least one comma-separated label")
    return labels


def _camera_check(capture_config: CaptureConfig) -> int:
    with OpenCVFrameSource(capture_config) as source:
        captured = source.read()
    _emit(
        {
            "event": "camera_frame_received",
            "source_kind": "rtsp" if capture_config.is_rtsp else "local_device",
            "width": captured.width,
            "height": captured.height,
            "frame_id": captured.frame_id,
            "image_saved": False,
            "hardware_control_enabled": False,
        }
    )
    return 0


def _run_live_camera(args: argparse.Namespace) -> int:
    config = MissionConfig.from_json(args.config)
    providers = tuple(args.provider)
    detectors = [
        OnnxNx6Detector(
            OnnxNx6Config(
                model_path=args.onnx_model,
                class_names=_parse_class_names(args.class_names),
                input_width=args.input_width,
                input_height=args.input_height,
                confidence_threshold=args.confidence_threshold,
                providers=providers,
            )
        )
    ]
    if args.safety_onnx_model is not None:
        detectors.append(
            OnnxNx6Detector(
                OnnxNx6Config(
                    model_path=args.safety_onnx_model,
                    class_names=_parse_class_names(args.safety_class_names),
                    input_width=args.input_width,
                    input_height=args.input_height,
                    confidence_threshold=args.confidence_threshold,
                    providers=providers,
                )
            )
        )
    detector = DetectorEnsemble(detectors)
    telemetry = (
        PixhawkReadOnlyTelemetryProvider(
            PixhawkReadOnlyConfig(endpoint=args.pixhawk_endpoint, baud=args.pixhawk_baud)
        )
        if args.pixhawk_endpoint
        else FailClosedTelemetryProvider()
    )
    controller = MissionController(config)
    runner = LiveMissionRunner(
        mission=controller,
        frame_source=OpenCVFrameSource(_capture_config_from_args(args)),
        detector=detector,
        telemetry_provider=telemetry,
        config=LiveRunConfig(
            operator_id=args.operator_id,
            max_frames=args.max_frames,
            display=not args.no_display,
        ),
    )
    _emit(
        {
            "event": "live_camera_started",
            "model_providers": [provider for item in detectors for provider in item.provider_names],
            "pixhawk_read_only": bool(args.pixhawk_endpoint),
            "physical_release_supported": False,
            "person_safety_model_coverage": detector.covers_labels(config.person_labels),
        }
    )
    result = runner.run()
    if args.audit_out is not None:
        args.audit_out.parent.mkdir(parents=True, exist_ok=True)
        controller.write_audit_jsonl(args.audit_out)
    _emit(
        {
            "event": "live_camera_finished",
            "processed_frames": result.processed_frames,
            "phase": result.final_phase.value,
            "authorizations": result.authorization_count,
            "audit_written": args.audit_out is not None,
            "physical_release_supported": False,
        }
    )
    return 0


def _run_replay(
    *,
    config_path: Path,
    replay_path: Path,
    simulate_authorized_cycle: bool,
    operator_id: str,
    audit_out: Path | None,
) -> int:
    config = MissionConfig.from_json(config_path)
    frames = load_jsonl_replay(replay_path)
    if not frames:
        raise ValueError("replay contains no frames")
    controller = MissionController(config)
    first_timestamp = frames[0].captured_at_s
    controller.launch(now_s=max(0.0, first_timestamp - 2.0))
    controller.arrive_task_area(now_s=max(0.0, first_timestamp - 1.0))
    completed_demo_cycle = False

    _emit(
        {
            "event": "replay_started",
            "mission_id": config.mission_id,
            "frame_count": len(frames),
            "simulation_only": True,
            "hardware_interfaces_present": False,
        }
    )
    for frame in frames:
        outcome = controller.process_observation(frame, now_s=frame.captured_at_s)
        _emit(
            {
                "event": "frame_evaluated",
                "frame_id": frame.frame_id,
                "phase": outcome.phase.value,
                "track_count": len(outcome.tracks),
                "decisions": [
                    {
                        "target_id": decision.target_id,
                        "allowed": decision.allowed,
                        "priority_score": decision.priority_score,
                        "denial_reasons": decision.denial_reasons,
                    }
                    for decision in outcome.decisions
                ],
                "simulation_only": True,
            }
        )
        challenge = outcome.challenge
        if challenge is None:
            continue
        _emit(
            {
                "event": "authorization_required",
                "challenge_id": challenge.challenge_id,
                "target_id": challenge.target_id,
                "target_revision": challenge.target_revision,
                "payload_slot_id": challenge.payload_slot_id,
                "scene_digest": challenge.scene_digest,
                "ruleset_version": challenge.ruleset_version,
                "expires_at_s": challenge.expires_at_s,
                "nonce_redacted": True,
                "simulation_only": True,
            }
        )
        if not simulate_authorized_cycle:
            break

        approved_at = frame.captured_at_s + 0.1
        controller.approve_authorization(
            challenge_id=challenge.challenge_id,
            nonce=challenge.nonce,
            operator_id=operator_id,
            now_s=approved_at,
        )
        _emit(
            {
                "event": "demo_operator_approved",
                "challenge_id": challenge.challenge_id,
                "operator_id": operator_id,
                "simulation_only": True,
            }
        )
        release_id = controller.request_simulated_deployment(now_s=approved_at + 0.1)
        controller.report_simulated_execution(release_id=release_id, now_s=approved_at + 0.2)
        controller.report_independent_confirmation(
            release_id=release_id,
            source_id="demo-independent-bay-sensor",
            now_s=approved_at + 0.3,
        )
        _emit(
            {
                "event": "simulated_release_confirmed",
                "release_id": release_id,
                "payload_slot_id": challenge.payload_slot_id,
                "remaining_payload_count": controller.payload.remaining_payload_count,
                "simulation_only": True,
            }
        )
        completed_demo_cycle = True
        break

    if audit_out is not None:
        audit_out.parent.mkdir(parents=True, exist_ok=True)
        controller.write_audit_jsonl(audit_out)
        _emit(
            {
                "event": "audit_written",
                "path": str(audit_out.resolve()),
                "event_count": len(controller.audit),
                "simulation_only": True,
            }
        )
    status = controller.status()
    _emit(
        {
            "event": "replay_finished",
            "phase": status.phase.value,
            "remaining_payload_count": status.remaining_payload_count,
            "pending_authorization": status.pending_challenge_id is not None,
            "simulated_cycle_completed": completed_demo_cycle,
            "fake_release_request_count": controller.fake_payload_port.request_count,
            "simulation_only": True,
        }
    )
    return 0


def _emit(document: dict[str, Any], *, stream: Any = None) -> None:
    destination = sys.stdout if stream is None else stream
    print(
        json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
        file=destination,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
