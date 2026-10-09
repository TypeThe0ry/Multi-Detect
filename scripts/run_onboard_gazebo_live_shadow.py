#!/usr/bin/env python3
"""Actual Gazebo RGB/state -> persistent VLM -> causal observation episode.

By default this is a no-process preflight. --execute enables the existing owned
Docker capture. --baseline-flight is a separate disposable SITL opt-in: the
existing r33/YOLO lifecycle flies, while the new verbatim Prompt is shadow-only.
The VLM never receives an aircraft transport or the baseline's execution grant.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from multidetect.autonomy.contracts import PromptEnvelope  # noqa: E402
from multidetect.autonomy.onboard_live_shadow import OnboardLiveShadowBridge  # noqa: E402
from multidetect.autonomy.onboard_live_vlm_backend import LiveQwenVlmBackendFactory  # noqa: E402
from multidetect.autonomy.onboard_vlm_worker import OnboardVlmProcessWorker  # noqa: E402
from multidetect.autonomy.serialization import (  # noqa: E402
    require_int,
    require_real,
    require_sha256,
)
from multidetect.product_version import PRODUCT_VERSION  # noqa: E402
from scripts.probe_onboard_vlm import require_model_identity  # noqa: E402
from scripts.run_simulator_docker import PINNED_IMAGE  # noqa: E402
from scripts.run_simulator_docker_acceptance import (  # noqa: E402
    run_acceptance as capture_acceptance,
)


def _record(path):
    path = Path(path).resolve(strict=True)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def _write_new(path, document):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def run(args, *, capture_runner=capture_acceptance, worker_factory=OnboardVlmProcessWorker):
    output = args.output_directory.resolve()
    if output.exists():
        raise FileExistsError("live run must not overwrite an existing directory")
    snapshot = args.snapshot.resolve(strict=True)
    require_model_identity("Qwen/Qwen3-VL-2B-Instruct", snapshot.name)
    weight_sha = require_sha256(args.model_sha256, "model_sha256")
    count = require_int(args.sensor_frames, "sensor_frames", minimum=2, maximum=10_000)
    timeout = require_real(args.timeout, "timeout", minimum=1, maximum=3600)
    require_real(args.deadline, "deadline", minimum=0.05, maximum=120)
    require_int(args.history_frames, "history_frames", minimum=1, maximum=4)
    require_int(args.max_total_pixels, "max_total_pixels", minimum=4, maximum=1_605_632)
    require_int(args.max_new_tokens, "max_new_tokens", minimum=1, maximum=1024)
    require_int(args.control_port, "control_port", minimum=1024, maximum=65535)
    require_int(args.telemetry_port, "telemetry_port", minimum=1024, maximum=65535)
    if args.baseline_flight and not args.execute:
        raise ValueError("baseline-flight requires explicit --execute")
    # The inherited baseline consumes a list and retains raw BGR + float32
    # depth for the run. This is a raw payload cap, not a peak-RAM promise.
    baseline_raw_bytes = count * 1280 * 960 * (3 + 4)
    if args.baseline_flight and baseline_raw_bytes > 1024 * 1024 * 1024:
        raise ValueError("baseline retained RGB/depth payload exceeds 1 GiB; reduce sensor-frames")
    if args.control_port == 14550 or args.telemetry_port == 14550:
        raise ValueError("QGC UDP port 14550 must remain untouched")
    if args.control_port == args.telemetry_port:
        raise ValueError("control and telemetry require separate loopback ports")
    original = args.prompt
    prompt = PromptEnvelope(
        str(uuid4()),
        original,
        "zh" if any("\u4e00" <= ch <= "\u9fff" for ch in original) else "en",
        datetime.now(timezone.utc).isoformat(),
        "local-simulator-operator",
        "owned-gazebo-multirotor",
    )
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "contract": "multidetect.onboard-gazebo-live-shadow-run-v1",
        "product_version": PRODUCT_VERSION.release_label,
        "prompt": prompt.to_mapping(),
        "prompt_text_original": original,
        "execution_requested": bool(args.execute),
        "baseline_flight_requested": bool(args.baseline_flight),
        "baseline_is_not_vlm_prompt_execution": True,
        "baseline_retained_raw_payload_bytes_estimate": baseline_raw_bytes
        if args.baseline_flight
        else 0,
        "baseline_retained_raw_payload_limit_bytes": 1024 * 1024 * 1024,
        "model_id": "Qwen/Qwen3-VL-2B-Instruct",
        "model_revision": snapshot.name,
        "model_sha256": weight_sha,
        "adapter_loaded": False,
        "capture": None,
        "shadow": None,
        "error": None,
        "cleanup_errors": [],
        "model_actuated_flight": False,
        "autonomous_flight_verified": False,
        "vlm_prompt_task_completed": False,
        "candidate_promoted": False,
        "flight_action_training_performed": False,
        "training_ready": False,
        "physical_performance_claimed": False,
        "hil_real_evidence": False,
        "jetson_evidence": False,
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
        "source_records": [
            _record(path)
            for path in (
                Path(__file__),
                ROOT / "src/multidetect/autonomy/onboard_live_shadow.py",
                ROOT / "src/multidetect/autonomy/onboard_live_episode.py",
                ROOT / "src/multidetect/autonomy/onboard_live_vlm_contract.py",
                ROOT / "src/multidetect/autonomy/onboard_live_vlm_backend.py",
                ROOT / "src/multidetect/autonomy/onboard_vlm_worker.py",
                ROOT / "src/multidetect/autonomy/onboard_vlm_runtime_contract.py",
                ROOT / "scripts/run_simulator_docker_acceptance.py",
                args.scenario,
            )
        ],
    }
    worker = bridge = None
    frames = []
    probes = []
    code = 1
    try:
        common = dict(
            execute=args.execute,
            timeout_s=timeout,
            docker=args.docker,
            image=PINNED_IMAGE,
            container_name=args.container_name,
            control_port=args.control_port,
            telemetry_port=args.telemetry_port,
            sensor_frame_count=count,
            sensor_width=1280,
            sensor_height=960,
            sensor_frame_output_dir=output / "rgb",
            sensor_depth_output_dir=output / "depth",
        )
        if not args.execute:
            report["capture"] = capture_runner(args.scenario, output / "capture.json", **common)
            report["status"] = "planned_not_executed"
            code = 0 if report["capture"].get("gate_passed") is True else 1
        else:
            factory = LiveQwenVlmBackendFactory(
                str(snapshot), weight_sha, 8192, args.max_new_tokens
            )
            worker = worker_factory(factory, startup_timeout_s=120)
            worker.start()
            warmed_at = time.monotonic()
            while worker.state == "starting" and time.monotonic() - warmed_at < 125:
                time.sleep(0.02)
            if worker.state != "idle":
                raise RuntimeError("VLM prewarm failed: " + str(worker.error or worker.state))
            bridge = OnboardLiveShadowBridge(
                worker,
                output_directory=output / "episode",
                prompt=prompt,
                prompt_text_original=original,
                history_frames=args.history_frames,
                deadline_s=args.deadline,
                max_total_pixels=args.max_total_pixels,
            )
            bridge.start()

            def on_frame(frame, provenance, depth):
                # Shadow receives every actual frame before old detector/task filtering.
                bridge.on_sensor_frame(frame, provenance, depth)
                if args.baseline_flight:
                    frames.append((frame, provenance, depth))

            common["sensor_frame_callback"] = on_frame
            if args.baseline_flight:
                from scripts.run_external_onboard_model_acceptance import (
                    DEFAULT_DETECTOR_MODEL,
                    DEFAULT_MODEL_REFERENCE,
                    ExternalOnboardDockerProbe,
                    _airframe_profile_hash,
                    _build_actual_perception_adapter,
                    _prepare_disposable_sitl_for_arm,
                    _resolve_docker_executable,
                    build_docker_simulator_plan,
                    load_onboard_model_reference,
                )

                ref = load_onboard_model_reference(DEFAULT_MODEL_REFERENCE, repository_root=ROOT)
                plan = build_docker_simulator_plan(
                    args.scenario,
                    repository_root=ROOT,
                    job_dir=output / "baseline-plan",
                    container_name=args.container_name,
                    control_port=args.control_port,
                    telemetry_port=args.telemetry_port,
                    image=PINNED_IMAGE,
                    dry_run=False,
                )
                capture_start = threading.Event()
                detector = _build_actual_perception_adapter(DEFAULT_DETECTOR_MODEL)

                def baseline_probe(endpoint):
                    probe = ExternalOnboardDockerProbe(
                        endpoint,
                        model_path=ref.model_path,
                        model_sha256=ref.model_sha256,
                        model_id=ref.model_id,
                        model_version=ref.model_version,
                        airframe_hash=_airframe_profile_hash(plan),
                        sensor_frames=frames,
                        frame_perception_adapter=detector,
                        execute_mission_transactions=True,
                        mission_journal_directory=output / "baseline-mission-journal",
                        execute_autonomous_flight=True,
                        autonomous_flight_timeout_s=20,
                        sensor_capture_start_event=capture_start,
                    )
                    probe.arm_preflight = _prepare_disposable_sitl_for_arm(
                        _resolve_docker_executable(args.docker),
                        args.container_name,
                        enable_joystick_control=False,
                    )
                    probes.append(probe)
                    return probe

                common.update(
                    telemetry_probe_factory=baseline_probe,
                    sensor_capture_concurrent=True,
                    sensor_capture_start_event=capture_start,
                )
                report["baseline_policy_reference"] = _record(DEFAULT_MODEL_REFERENCE)
            report["capture"] = capture_runner(args.scenario, output / "capture.json", **common)
            report["shadow"] = bridge.finish(timeout_s=args.deadline + 5)
            shadow_ok = (
                report["shadow"]["closed"]
                and report["shadow"]["error"] is None
                and report["shadow"]["strict_decisions"] > 0
                and report["shadow"]["queue_overflow_frames"] == 0
                and report["shadow"]["recorded_frames"] == count
            )
            baseline_ok = not args.baseline_flight or (
                probes and probes[-1].report_completion_observed
            )
            code = (
                0
                if report["capture"].get("gate_passed") is True and shadow_ok and baseline_ok
                else 1
            )
            report["status"] = "capture_and_shadow_passed" if code == 0 else "failed_partial"
            report["baseline_probe"] = None if not probes else probes[-1].observe_result
    except Exception as exc:
        code = 1
        report["error"] = type(exc).__name__ + ": " + str(exc)
        report["status"] = "failed_partial"
    finally:
        if bridge is not None and report["shadow"] is None:
            try:
                report["shadow"] = bridge.finish(timeout_s=args.deadline + 5)
            except Exception as exc:
                report["cleanup_errors"].append(
                    "bridge.finish: " + type(exc).__name__ + ": " + str(exc)
                )
                try:
                    report["shadow"] = bridge.evidence()
                except Exception as evidence_error:
                    report["cleanup_errors"].append("bridge.evidence: " + str(evidence_error))
        if worker is not None:
            try:
                worker.close()
            except Exception as exc:
                report["cleanup_errors"].append(
                    "worker.close: " + type(exc).__name__ + ": " + str(exc)
                )
        report["source_recheck"] = []
        for row in report["source_records"]:
            try:
                current = _record(row["path"])
                report["source_recheck"].append({"path": row["path"], "unchanged": current == row})
            except Exception as exc:
                report["source_recheck"].append(
                    {
                        "path": row["path"],
                        "unchanged": False,
                        "error": type(exc).__name__ + ": " + str(exc),
                    }
                )
        report["source_records_unchanged"] = all(
            row["unchanged"] for row in report["source_recheck"]
        )
        if report["cleanup_errors"] or not report["source_records_unchanged"]:
            code = report["native_exit_code"] = 1
            report["status"] = "failed_partial"
        report["native_exit_code"] = code
        _write_new(output / "report.json", report)
    return code


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--model-sha256", required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument(
        "--scenario",
        type=Path,
        default=ROOT / "configs/scenarios/urban_inspection_multirotor_001.scenario.json",
    )
    parser.add_argument("--docker", type=Path)
    parser.add_argument("--container-name", default="multidetect-live-vlm-shadow")
    parser.add_argument("--control-port", type=int, default=24570)
    parser.add_argument("--telemetry-port", type=int, default=24571)
    parser.add_argument("--sensor-frames", type=int, default=64)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--deadline", type=float, default=30)
    parser.add_argument("--history-frames", type=int, default=2)
    parser.add_argument("--max-total-pixels", type=int, default=200_704)
    parser.add_argument("--max-new-tokens", type=int, default=384)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--baseline-flight", action="store_true")
    return parser


def main():
    return run(_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
