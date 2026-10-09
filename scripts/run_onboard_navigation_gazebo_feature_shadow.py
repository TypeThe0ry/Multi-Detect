#!/usr/bin/env python3
"""New R3 diagnostic capture of the exact live feature passed to the saved head.

Reuse the frozen R2 loader, native prefix projection and owned input-only Gazebo
lifecycle. No second forward, training, flight transport or default promotion.
The post-inference feature receipt is not renewed freshness/flight authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from scripts import onboard_navigation_live_shadow_plan as plans  # noqa: E402
from scripts import run_onboard_navigation_gazebo_learned_shadow_v2 as previous  # noqa: E402

CONTRACT = "multidetect.onboard-navigation-gazebo-feature-shadow-r3"
PRIOR_PLAN = "configs/training/onboard-navigation-live-shadow-20261008-r2.json"
PRIOR_PLAN_SHA = "87fa8e48a6a42e34d542a5d29e70050a2cfdec254a46159b886b59fdf034d44e"
PRIOR_REPORT = "artifacts/evaluation/onboard-navigation-live-shadow-20261008-r2/worker-report.json"
PRIOR_REPORT_SHA = "034ab64c239348d66c7adbdc048adb94815f8ed950b6eb195a286b68767bf6e5"
NEW_FILES = (
    "scripts/run_onboard_navigation_gazebo_feature_shadow.py",
    "tests/test_onboard_navigation_gazebo_feature_shadow.py",
    "scripts/onboard_navigation_feature_capture.py",
    "tests/test_onboard_navigation_feature_capture.py",
)


def require_r3_sources(plan):
    """The reused plan contract must explicitly bind every new implementation."""
    source_paths = {row["path"] for row in plan["source_files"]}
    for relative in NEW_FILES:
        if (ROOT / relative).resolve().as_posix() not in source_paths:
            raise ValueError("R3 source or inherited evidence missing from plan: " + relative)
    for relative, digest in ((PRIOR_PLAN, PRIOR_PLAN_SHA), (PRIOR_REPORT, PRIOR_REPORT_SHA)):
        binding = previous.record(ROOT / relative)
        if binding["sha256"] != digest:
            raise ValueError("Frozen R2 evidence changed")
    return True


def build_feature_plan(output_directory):
    """Keep the prior prompt, exact candidate/head/norm and zero-training budget."""
    # Completed evidence has an existing output directory; the original plan
    # admission deliberately requires a fresh output and cannot be replayed.
    # Verify its frozen bytes here, then admit all copied settings in a NEW plan.
    prior_bytes = (ROOT / PRIOR_PLAN).read_bytes()
    if hashlib.sha256(prior_bytes).hexdigest() != PRIOR_PLAN_SHA:
        raise ValueError("Frozen R2 plan changed")
    old = plans._json(prior_bytes)
    plan = plans.build_plan(
        output_directory,
        prompt_text_original=old["prompt_text_original"],
        capture_runtime=old["capture_runtime"],
        load_config=old["load_config"],
        extra_source_files=(*old["extra_source_files"], *NEW_FILES),
    )
    require_r3_sources(plan)
    return plan


def verify_feature_closure(observations, plan_sha256, *, reader=None, file_record=None):
    """Check actual payloads, not merely eight callbacks or eight acceptances."""
    from scripts.onboard_navigation_feature_capture import canonical

    reader = plans._json if reader is None else reader
    file_record = previous.record if file_record is None else file_record
    import struct

    for row in observations:
        receipt_binding = row.get("metrics", {}).get("live_feature_receipt")
        if receipt_binding is None or file_record(receipt_binding["path"]) != receipt_binding:
            raise ValueError("Every request requires its unchanged same-call feature receipt")
        receipt = reader(Path(receipt_binding["path"]).read_bytes())
        if (
            receipt["experiment_plan_sha256"] != plan_sha256
            or receipt["request_sha256"] != row["request_sha256"]
            or receipt["backend_gate_accepted"] is not row["accepted"]
            or receipt["capture_error"] is not None
            or receipt["unadmitted_partial_files"]
            or not receipt["predictor_returned"]
            or not receipt["original_head_call_attempted"]
        ):
            raise ValueError("Incomplete or crossed same-call feature capture")
        for key in ("feature", "six_output"):
            payload = receipt[key]
            if payload is None or file_record(payload["file"]["path"]) != payload["file"]:
                raise ValueError("Both actual feature and six output payloads required")
        feature = receipt["feature"]
        if (
            feature["file"]["bytes"] != 8192
            or feature["file"]["sha256"] != (row["metrics"]["actual_feature"]["sha256"])
        ):
            raise ValueError("Actual feature bytes differ from backend record")
        six = receipt["six_output"]
        values = six["values"]
        disk_six = reader(Path(six["file"]["path"]).read_bytes())
        if set(disk_six) != {"tensor", "values", "source"} or (
            disk_six["source"] != "actual_same_call_head_output"
            or canonical(disk_six["values"]) != canonical(values)
            or disk_six["tensor"] != six["tensor"]
        ):
            raise ValueError("Six output file differs from same-call receipt values")
        if type(values) is not list or len(values) != 1 or len(values[0]) != 6:
            raise ValueError("Exact same-call six FP32 values required")
        raw_six = struct.pack("<6f", *values[0])
        if hashlib.sha256(raw_six).hexdigest() != six["tensor"]["sha256"]:
            raise ValueError("Actual six output values differ from tensor hash")
        if six["tensor"] != row["metrics"]["actual_six_output"]:
            raise ValueError("Six output tensor differs from the backend record")
        if canonical(receipt["raw_prediction"]) != canonical(row["raw_prediction"]):
            raise ValueError("Receipt raw prediction differs from journal observation")
        diagnostics = {
            key: value for key, value in row["metrics"].items() if key != "live_feature_receipt"
        }
        if (
            hashlib.sha256(canonical(diagnostics)).hexdigest()
            != (receipt["backend_diagnostics_sha256"])
        ):
            raise ValueError("Receipt diagnostic snapshot differs from the backend gate")
    return len(observations)


class FeatureRecordingBackend:
    """One backend call and one durable outcome per original request."""

    def __init__(self, backend, recorder):
        self.backend, self.recorder = backend, recorder

    def infer(self, request):
        from scripts.onboard_navigation_inference_backend import NavigationBackendRejected

        self.recorder.begin(request)
        try:
            answer = self.backend.infer(request)
        except NavigationBackendRejected as exc:
            diagnostics = json.loads(exc.diagnostics_json)
            capture_error = self.recorder.capture_error
            binding = self.recorder.commit_outcome(
                request,
                accepted=False,
                diagnostics=diagnostics,
                raw_prediction=diagnostics.get("raw_prediction"),
            )
            diagnostics["live_feature_receipt"] = binding
            if capture_error is not None:
                raise RuntimeError(
                    "Same-call capture failed; diagnostic receipt retained at " + binding["path"]
                ) from exc
            raise NavigationBackendRejected(str(exc), diagnostics) from exc
        except BaseException as exc:
            try:
                self.recorder.commit_outcome(
                    request,
                    accepted=False,
                    diagnostics={"error_type": type(exc).__name__, "result_accepted": False},
                    raw_prediction=None,
                )
            except BaseException as receipt_error:
                exc.add_note("Feature failure receipt also failed: " + str(receipt_error))
            raise
        if answer["metrics"].get("result_accepted") is not True:
            raise ValueError("Accepted backend response must carry its actual gate outcome")
        binding = self.recorder.commit_outcome(
            request,
            accepted=True,
            diagnostics=answer["metrics"],
            raw_prediction=answer["raw_prediction"],
        )
        answer["metrics"]["live_feature_receipt"] = binding
        return answer


def worker(plan_path, plan_sha256):
    plan, binding = plans.admit_plan(plan_path, plan_sha256)
    require_r3_sources(plan)
    output = Path(plan["output_dir"])
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "contract": CONTRACT,
        "pid": os.getpid(),
        "plan": binding,
        "success": False,
        "native_exit_code": 1,
        "claims": dict(previous.FALSE_CLAIMS),
        "loader": {},
        "capture_dependencies": {},
        "observations": [],
        "feature_receipts": [],
        "source_bindings_before": None,
        "inherited_R1_R2_camera_requests": 16,
        "inherited_R1_R2_fused_forward_returns": 8,
        "inherited_R2_evidence": [
            previous.record(ROOT / path) for path in (PRIOR_PLAN, PRIOR_REPORT)
        ],
        "inherited_head_optimizer_attempts": 3000,
        "inherited_base_lora_optimizer_attempts": 280,
        "new_optimizer_attempts": 0,
        "new_generation_calls": 0,
        "feature_receipt_is_new_freshness_or_execution_authority": False,
    }
    error = None
    sink = recorder = None
    try:
        report["source_bindings_before"] = plans.capture_sources(plan)
        intent_path = output.with_name(output.name + "-ownership.json")
        intent = plans._json(intent_path.read_bytes())
        if (
            intent.get("plan_sha256") != plan_sha256
            or intent.get("image") != previous.PINNED_IMAGE
            or intent.get("container_name") != "multidetect-navigation-input-capture"
            or re.fullmatch(r"[0-9a-f]{32}", intent.get("token", "")) is None
        ):
            raise ValueError("Supervisor ownership intent differs")
        report["ownership_intent"] = previous.record(intent_path)
        from scripts.onboard_navigation_live_loader import load_preloaded

        with load_preloaded(plan["load_config"], observations=report["loader"]) as loaded:
            print(json.dumps({"event": "exact_R3_model_loaded", "pid": os.getpid()}), flush=True)
            with previous.scoped_capture_dependencies(
                plan["capture_runtime"], report["capture_dependencies"]
            ):
                from scripts import run_gazebo_navigation_input_capture as capture
                from scripts import run_simulator_docker_acceptance as original
                from scripts.onboard_navigation_feature_capture import FeatureCaptureRecorder
                from scripts.onboard_navigation_gazebo_shadow_input import (
                    GazeboNavigationInputBuffer,
                )
                from scripts.onboard_navigation_learned_readout_backend import (
                    PreloadedLearnedNavigationBackend,
                )
                from scripts.onboard_navigation_native_prefix_adapter import (
                    canonical_native_prefix_processor,
                )

                service = None

                def make_buffer(prompt, **kwargs):
                    nonlocal service, sink, recorder
                    if service is not None:
                        raise RuntimeError("Only one capture clock/backend allowed")
                    recorder = FeatureCaptureRecorder(
                        output / "features",
                        experiment_plan_sha256=plan_sha256,
                        engine_identity=loaded.engine_identity,
                        readout_identity=loaded.readout_identity,
                        readout_metadata=loaded.readout_metadata,
                        max_requests=plan["capture"]["sensor_frames"],
                    )
                    backend = PreloadedLearnedNavigationBackend(
                        loaded.engine,
                        loaded.engine_identity,
                        loaded.head,
                        loaded.normalization,
                        loaded.readout_identity,
                        loaded.readout_metadata,
                        clock=kwargs["clock"],
                        prefix_processor=canonical_native_prefix_processor,
                        head_predictor=recorder.head_predictor,
                    )
                    service = FeatureRecordingBackend(backend, recorder)
                    sink = previous.ShadowRequestSink(
                        service, output / "observations.jsonl", clock=kwargs["clock"]
                    )
                    report["observations"] = sink.rows
                    report["clock_identity"] = kwargs["host_clock_identity"]
                    return GazeboNavigationInputBuffer(prompt, **kwargs)

                cli = previous.owned_docker_runner(
                    output / "owned-container.json", capture.CONTAINER_NAME, intent
                )

                def run_capture(*args, **kwargs):
                    return original.run_acceptance(*args, **kwargs, runner=cli)

                def callback(prepared):
                    if sink is None:
                        raise RuntimeError("Capture clock was not bound")
                    sink(prepared)

                options = plan["capture"]
                result = capture.run_input_capture(
                    options["source"],
                    output / "capture/capture.json",
                    output / "capture",
                    prompt=plan["prompt_text_original"],
                    model_sha256=loaded.engine_identity.model_sha256,
                    adapter_sha256=loaded.engine_identity.adapter_sha256,
                    adapter_config_sha256=loaded.engine_identity.adapter_config_sha256,
                    execute=True,
                    sensor_frames=options["sensor_frames"],
                    timeout_s=options["timeout_s"],
                    control_port=options["control_port"],
                    telemetry_port=options["telemetry_port"],
                    request_callback=callback,
                    buffer_factory=make_buffer,
                    capture_runner=run_capture,
                    capture_module=original,
                )
                report["capture_input_report"] = previous.record(
                    output / "capture/input-report.json"
                )
                report["capture_success"] = result["success"]
                report["observations"] = sink.rows if sink else []
                if not result["success"] or len(report["observations"]) != options["sensor_frames"]:
                    raise ValueError("Every precommitted camera request must close")
        report["source_bindings_after"] = plans.capture_sources(plan)
        if report["source_bindings_before"] != report["source_bindings_after"]:
            raise ValueError("R3 source/runtime bindings changed")
        report["source_bindings_unchanged"] = True
        report["feature_capture_complete_count"] = verify_feature_closure(
            report["observations"], plan_sha256
        )
        report["feature_capture_complete"] = (
            report["feature_capture_complete_count"] == plan["capture"]["sensor_frames"]
        )
        if report["feature_capture_complete"] is not True:
            raise ValueError("All precommitted live feature payloads must close")
        report["success"], report["native_exit_code"] = True, 0
    except BaseException as exc:
        error = exc
        report["error"] = {"type": type(exc).__name__, "message": str(exc)[:2048]}
    finally:
        report["accepted_count"] = sum(row["accepted"] for row in report["observations"])
        report["rejected_count"] = len(report["observations"]) - report["accepted_count"]
        report["model_output_quality_passed"] = False
        report["feature_receipts"] = [
            row["metrics"]["live_feature_receipt"]
            for row in report["observations"]
            if "live_feature_receipt" in row.get("metrics", {})
        ]
        if (output / "observations.jsonl").is_file():
            report["raw_journal"] = previous.record(output / "observations.jsonl")
        previous.write_new(output / "worker-report.json", report)
    if error is not None and not isinstance(error, Exception):
        raise error
    return report["native_exit_code"]


def supervise(plan_path, plan_sha256):
    plan, binding = plans.admit_plan(plan_path, plan_sha256)
    require_r3_sources(plan)
    output = Path(plan["output_dir"])
    if output.exists() or plan["worker_timeout_s"] != 600:
        raise ValueError("Fresh output and fixed 600-second watchdog required")
    from scripts.run_gazebo_navigation_input_capture import CONTAINER_NAME

    docker = shutil.which("docker")
    if not docker or previous.docker_inspect(docker, CONTAINER_NAME) is not None:
        raise ValueError("Docker ready and owned capture name absent required")
    report_path = output.with_name(output.name + "-supervisor.json")
    intent_path = output.with_name(output.name + "-ownership.json")
    stdout_path, stderr_path = output.with_suffix(".stdout.log"), output.with_suffix(".stderr.log")
    for path in (report_path, intent_path, stdout_path, stderr_path):
        if path.exists():
            raise FileExistsError("Fresh supervisor outputs required")
    output.parent.mkdir(parents=True, exist_ok=True)
    previous.write_new(
        intent_path,
        {
            "plan_sha256": plan_sha256,
            "container_name": CONTAINER_NAME,
            "image": previous.PINNED_IMAGE,
            "token": uuid4().hex,
            "supervisor_pid": os.getpid(),
        },
    )
    report = {
        "contract": CONTRACT + "-supervisor",
        "plan": binding,
        "success": False,
        "worker_timeout_s": 600,
        "watchdog_fired": False,
        "claims": dict(previous.FALSE_CLAIMS),
        "container_absent_before": True,
        "ownership_intent": previous.record(intent_path),
    }
    child = None
    try:
        command = [
            plan["load_config"]["runtime"]["python_executable"],
            "-I",
            "-B",
            "-u",
            "-X",
            "utf8",
            str(Path(__file__).resolve()),
            "--worker",
            "--plan",
            str(Path(plan_path).resolve()),
            "--plan-sha256",
            plan_sha256,
        ]
        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            child = subprocess.Popen(command, cwd=ROOT, stdout=stdout, stderr=stderr)
            report["worker_pid"] = child.pid
            print(
                json.dumps(
                    {
                        "event": "R3_feature_worker_started",
                        "pid": child.pid,
                        "stdout": str(stdout_path),
                        "stderr": str(stderr_path),
                    }
                ),
                flush=True,
            )
            try:
                report["worker_exit_code"] = child.wait(timeout=600)
            except subprocess.TimeoutExpired:
                report["watchdog_fired"] = True
                report["termination"] = previous.terminate_owned_tree(child)
                report["worker_exit_code"] = child.returncode
    except BaseException as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)[:2048]}
        if child is not None:
            try:
                report["termination"] = previous.terminate_owned_tree(child)
            except Exception as termination_error:
                report["termination_error"] = {
                    "type": type(termination_error).__name__,
                    "message": str(termination_error),
                }
    finally:
        report["worker_wait_completed"] = child is not None and child.poll() is not None
        previous.observe_owned_cleanup(docker, CONTAINER_NAME, output, report)
        try:
            if (output / "worker-report.json").is_file():
                report["worker_report"] = previous.record(output / "worker-report.json")
                closed = plans._json((output / "worker-report.json").read_bytes())
                report["success"] = (
                    report.get("worker_exit_code") == 0
                    and closed["success"] is True
                    and report["worker_wait_completed"]
                    and report["container_absent_after"]
                    and not report["watchdog_fired"]
                    and not report.get("outer_owned_cleanup_errors")
                    and not any(
                        key in report
                        for key in ("outer_cleanup_error", "error", "termination_error")
                    )
                )
            report["stdout"], report["stderr"] = (
                previous.record(stdout_path),
                previous.record(stderr_path),
            )
        except Exception as exc:
            report["success"] = False
            report["final_validation_error"] = {"type": type(exc).__name__, "message": str(exc)}
        previous.write_new(report_path, report)
    print(
        json.dumps(
            {
                "event": "R3_feature_supervisor_closed",
                "success": report["success"],
                "report": str(report_path),
            }
        ),
        flush=True,
    )
    return 0 if report["success"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--plan-sha256")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--emit-plan", action="store_true")
    parser.add_argument("--output-directory", type=Path)
    args = parser.parse_args(argv)
    if args.emit_plan:
        if args.worker or args.plan_sha256 or args.output_directory is None:
            parser.error(
                "Plan emission requires a fresh output directory and no execution arguments"
            )
        result = plans.write_plan(args.plan, build_feature_plan(args.output_directory))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.plan_sha256 is None or args.output_directory is not None:
        parser.error("Execution requires only admitted plan path and explicit SHA")
    return (worker if args.worker else supervise)(args.plan, args.plan_sha256)


if __name__ == "__main__":
    raise SystemExit(main())
