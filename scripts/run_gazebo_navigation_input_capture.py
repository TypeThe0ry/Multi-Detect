#!/usr/bin/env python3
"""Owned Gazebo RGB/depth capture to navigation requests; INPUT ONLY.

This standalone entry must exclusively own its process. A scoped invocation
adapter adds an explicit HighResolutionSystemClock to the frozen capture helper;
it neither edits sources nor relabels legacy timestamps. Its lock prevents two
adapter invocations, NOT unrelated threads calling the legacy module directly.
No model, processor, inference backend, optimizer or MissionRunner is created.
Optional request_callback is caller-owned and makes no inference claim here.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from multidetect.autonomy.contracts import PromptEnvelope  # noqa: E402
from multidetect.autonomy.serialization import require_int, require_real  # noqa: E402
from multidetect.clock import HighResolutionSystemClock  # noqa: E402
from scripts.onboard_navigation_gazebo_shadow_input import (  # noqa: E402
    GazeboNavigationInputBuffer,
    PreparedGazeboNavigationRequest,
)
from scripts.onboard_navigation_inference_backend import NavigationEngineIdentity  # noqa: E402

CONTRACT = "multidetect.gazebo-navigation-input-capture-v1"
CONTAINER_NAME = "multidetect-navigation-input-capture"
DEFAULT_SOURCE = ROOT / "configs/scenarios/urban_inspection_multirotor_001.scenario.json"
_ADAPTER_LOCK = threading.Lock()


class _CaptureInterruption(RuntimeError):
    """Private ordinary exception so frozen lifecycle reaches its owned cleanup."""


FALSE_CLAIMS = {
    "model_loaded": False,
    "ML_or_processor_executed_here": False,
    "inference_performed_here": False,
    "optimizer_performed_here": False,
    "model_quality_verified": False,
    "model_identity_authenticated_here": False,
    "head_bundle_loaded_here": False,
    "flight_actuated_here": False,
    "mission_runner_used": False,
    "sensor_authenticity_verified": False,
    "quality_claim": False,
    "candidate_promoted": False,
    "execution_authority": False,
    "training_ready": False,
    "physical_performance_claimed": False,
    "hil_real_evidence": False,
    "final_release_allowed": False,
}


def _record(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Small source/evidence file required")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(block)
    return {"path": path.as_posix(), "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def _sources(source):
    paths = (
        Path(__file__),
        ROOT / "tests/test_gazebo_navigation_input_capture.py",
        ROOT / "scripts/onboard_navigation_gazebo_shadow_input.py",
        ROOT / "scripts/onboard_navigation_inference_backend.py",
        ROOT / "scripts/run_simulator_docker_acceptance.py",
        ROOT / "src/multidetect/clock.py",
        ROOT / "src/multidetect/backends/gazebo_sim.py",
        ROOT / "src/multidetect/autonomy/onboard_navigation_actor.py",
        ROOT / "src/multidetect/autonomy/onboard_navigation_inference.py",
        source,
    )
    return [_record(path) for path in paths]


def _write_new(path, value):
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    with Path(path).open("xb") as stream:
        if stream.write(raw) != len(raw):
            raise OSError("Short input report write")
        stream.flush()
        os.fsync(stream.fileno())


@contextmanager
def scoped_capture_clock(module, clock, observation, *, sensor_capture_concurrent=False):
    """Exclusive invocation scope, with original helper restored even on BaseException."""
    if sensor_capture_concurrent is not False:
        raise ValueError("Concurrent capture is not supported by this standalone adapter")
    if type(clock) is not HighResolutionSystemClock:
        raise TypeError("Explicit HighResolutionSystemClock required")
    if not _ADAPTER_LOCK.acquire(blocking=False):
        raise RuntimeError("Nested or concurrent capture adapter denied")
    original = None
    interruptions = []
    try:
        original = module._capture_gazebo_sensor_transport
        owner = threading.get_ident()

        def with_clock(*args, **kwargs):
            if threading.get_ident() != owner:
                raise RuntimeError("Capture helper invoked outside the exclusive owner thread")
            if "clock" in kwargs:
                raise ValueError("Existing capture clock must not be overridden")
            observation["capture_helper_calls"] += 1
            try:
                return original(*args, **kwargs, clock=clock)
            except (KeyboardInterrupt, SystemExit) as exc:
                interruptions.append(exc)
                observation["helper_interruptions_bridged"] = len(interruptions)
                raise _CaptureInterruption(
                    "Capture interrupted; owned lifecycle cleanup required"
                ) from exc

        module._capture_gazebo_sensor_transport = with_clock
        observation["scoped_capture_override_installed"] = True
        yield interruptions
    finally:
        if original is not None:
            observation["scoped_capture_override_identity_intact_before_restore"] = (
                module._capture_gazebo_sensor_transport is with_clock
            )
            module._capture_gazebo_sensor_transport = original
            observation["scoped_capture_override_restored"] = (
                module._capture_gazebo_sensor_transport is original
            )
        _ADAPTER_LOCK.release()


def run_input_capture(
    source,
    evidence_path,
    output_dir,
    *,
    prompt,
    model_sha256,
    adapter_sha256,
    adapter_config_sha256,
    execute=False,
    control_port=24580,
    telemetry_port=24581,
    timeout_s=120,
    sensor_frames=8,
    docker=None,
    request_callback=None,
    capture_runner=None,
    capture_module=None,
    buffer_factory=GazeboNavigationInputBuffer,
):
    """Return input-report; source/capture ownership is delegated, never inference.

    request_callback receives the actual PreparedGazeboNavigationRequest, not
    just a lossy JSON reconstruction. Prepared source observations are retained
    before calling it. Its exception rejects this capture; no callback retries.
    Dependencies injected into fixtures are not actual external acceptance.
    """
    if type(execute) is not bool:
        raise TypeError("execute must be bool")
    if request_callback is not None and not callable(request_callback):
        raise TypeError("request_callback must be callable or None")
    identity = NavigationEngineIdentity(model_sha256, adapter_sha256, adapter_config_sha256)
    count = require_int(sensor_frames, "sensor_frames", minimum=1, maximum=8)
    timeout = require_real(timeout_s, "timeout_s", minimum=1, maximum=120)
    control = require_int(control_port, "control_port", minimum=1024, maximum=65535)
    telemetry = require_int(telemetry_port, "telemetry_port", minimum=1024, maximum=65535)
    if control == telemetry or 14550 in (control, telemetry):
        raise ValueError("Distinct isolated ports required; QGC 14550 is untouched")
    source = Path(source).resolve(strict=True)
    output = Path(output_dir).resolve()
    evidence = Path(evidence_path).resolve()
    if output.exists() or evidence.exists():
        raise FileExistsError("Input capture outputs must be fresh/create-only")
    if (
        output == ROOT
        or ROOT / "artifacts" not in output.parents
        or evidence.parent != output
        or evidence.name == "input-report.json"
        or evidence.suffix != ".json"
    ):
        raise ValueError("Fresh artifacts child and separate direct-child capture JSON required")
    envelope = PromptEnvelope(
        str(uuid4()),
        prompt,
        "zh" if any("\u4e00" <= ch <= "\u9fff" for ch in prompt) else "en",
        datetime.now(timezone.utc).isoformat(),
        "local-simulator-input-observer",
        "owned-gazebo-multirotor",
    )
    before = _sources(source)
    output.mkdir(parents=True, exist_ok=False)
    clock = HighResolutionSystemClock(session_id="navigation-input-capture-" + uuid4().hex)
    buffer = buffer_factory(
        envelope,
        prompt_text_original=prompt,
        host_clock_identity=clock.identity(),
        clock=clock.monotonic,
        max_frame_age_s=5.0,
        deadline_s=5.0,
    )
    scope = {
        "kind": "scoped_invocation_adapter_not_source_modification",
        "exclusive_standalone_process_required": True,
        "global_thread_safety_claimed": False,
        "legacy_timestamps_relabelled": False,
        "scoped_capture_override_installed": False,
        "scoped_capture_override_restored": False,
        "scoped_capture_override_identity_intact_before_restore": False,
        "capture_helper_calls": 0,
        "helper_interruptions_bridged": 0,
    }
    report = {
        "schema_version": 1,
        "contract": CONTRACT,
        "pid": os.getpid(),
        "success": False,
        "native_exit_code": 1,
        "execution_requested": execute,
        "status": "not_started",
        "source": _record(source),
        "prompt_text_original": prompt,
        "prompt_envelope": envelope.to_mapping(),
        "clock_identity": clock.identity(),
        "scoped_capture_adapter": scope,
        "external_callback_requested": request_callback is not None,
        "external_callback_completed_count": 0,
        "prepared_request_count": 0,
        "prepared_requests": [],
        "model_file_identity_role": "merely_declared_not_loaded",
        "declared_model_files": {
            name: getattr(identity, name) for name in identity.__dataclass_fields__
        },
        "sensor_frames_requested": count,
        "sensor_dimensions": [1280, 960],
        "maximum_RGB_and_depth_payload_bytes": count * 1280 * 960 * 7,
        "payload_cap_is_not_peak_RAM_bound": True,
        "source_bindings_before": before,
        "source_bindings_after": None,
        "source_bindings_unchanged": False,
        "source_scope": "ten_small_entrypoints_and_scenario_not_transitive_assets",
        "capture": None,
        "capture_evidence": None,
        "error": None,
        "claims": dict(FALSE_CLAIMS),
    }

    frame_error = None
    last_prepared_at = None

    def on_frame(frame, provenance, _depth):
        nonlocal frame_error
        try:
            prepare_frame(frame, provenance)
        except BaseException as exc:
            frame_error = exc
            raise

    def prepare_frame(frame, provenance):
        nonlocal last_prepared_at
        if len(report["prepared_requests"]) >= count:
            raise ValueError("Capture callback exceeded precommitted frame cap")
        buffer.offer(frame, provenance)
        prepared = buffer.build_request(identity, generation=len(report["prepared_requests"]) + 1)
        if type(prepared) is not PreparedGazeboNavigationRequest:
            raise TypeError("Typed PreparedGazeboNavigationRequest required")
        prepared.__post_init__()
        request = prepared.request
        row = {
            "request": request.to_mapping(),
            "request_sha256": request.canonical_hash,
            "actor_identity_sha256": request.actor_identity_sha256,
            "source_receipt_json": prepared.source_receipt_json,
            "source_binding_sha256": request.source_binding_sha256,
            "inference_performed_here": False,
        }
        # Binding validation above can decode RGB. Sample the SAME source epoch
        # after it, with a final non-decoding freshness check before publishing.
        now = require_real(clock.monotonic(), "prepared host time", minimum=0)
        if (
            (last_prepared_at is not None and now < last_prepared_at)
            or not request.submitted_at_s <= now < request.deadline_s
            or any(
                not 0 <= now - frame.captured_at_s <= request.max_frame_age_s
                for frame in request.source_frames
            )
        ):
            raise ValueError("Prepared request is future, stale, expired or clock-regressed")
        last_prepared_at = now
        row["prepared_at_s"] = now
        row["freshness_checked_before_callback"] = True
        report["prepared_requests"].append(row)
        report["prepared_request_count"] = len(report["prepared_requests"])
        if request_callback is not None:
            request_callback(prepared)
            report["external_callback_completed_count"] += 1

    pending_base_error = None
    interruptions = []
    try:
        module = capture_module or importlib.import_module(
            "scripts.run_simulator_docker_acceptance"
        )
        runner = module.run_acceptance if capture_runner is None else capture_runner
        with scoped_capture_clock(module, clock, scope) as interruptions:
            capture = runner(
                source,
                evidence,
                execute=execute,
                timeout_s=timeout,
                docker=docker,
                container_name=CONTAINER_NAME,
                control_port=control,
                telemetry_port=telemetry,
                sensor_frame_count=count,
                sensor_width=1280,
                sensor_height=960,
                sensor_frame_output_dir=output / "rgb",
                sensor_depth_output_dir=output / "depth",
                sensor_frame_callback=on_frame,
                sensor_capture_concurrent=False,
                sensor_capture_start_event=None,
                mission_transaction_runner=None,
            )
            report["capture"] = capture
        if frame_error is not None:
            raise frame_error
        if interruptions:
            raise interruptions[0]
        if scope["scoped_capture_override_identity_intact_before_restore"] is not True:
            raise ValueError("Scoped capture helper identity changed before restoration")
        if capture.get("gate_passed") is not True:
            raise ValueError("Owned Docker capture/preflight gate did not pass")
        if capture.get("execution_requested") is not execute:
            raise ValueError("Capture execution mode differs")
        if execute:
            if report["prepared_request_count"] != count or scope["capture_helper_calls"] != 1:
                raise ValueError("Exactly one capture and every requested frame must be prepared")
            if (
                request_callback is not None
                and report["external_callback_completed_count"] != count
            ):
                raise ValueError("Every requested external callback must return normally")
            safety = capture["safety"]
            if (
                safety["flight_control_output_enabled"] is not False
                or type(safety["flight_control_commands_sent"]) is not int
                or safety["flight_control_commands_sent"] != 0
                or capture["mission_transaction"]["requested"] is not False
                or capture["sensor_capture"]["concurrent"] is not False
            ):
                raise ValueError(
                    "Input-only capture must not actuate or use Mission/concurrent capture"
                )
        elif report["prepared_request_count"] or scope["capture_helper_calls"]:
            raise ValueError("Preflight must not prepare runtime observations")
        report["success"] = True
        report["native_exit_code"] = 0
        report["status"] = "input_capture_closed" if execute else "preflight_not_executed"
    except BaseException as exc:
        # A runner/cleanup secondary error must not replace the callback's cause.
        exc = (
            frame_error if frame_error is not None else (interruptions[0] if interruptions else exc)
        )
        report["error"] = {"type": type(exc).__name__, "message": str(exc)[:2048]}
        report["status"] = "failed_partial"
        if not isinstance(exc, Exception):
            pending_base_error = exc
    finally:
        try:
            after = _sources(source)
            report["source_bindings_after"] = after
            report["source_bindings_unchanged"] = after == before
            if evidence.is_file():
                report["capture_evidence"] = _record(evidence)
        except Exception as exc:
            report["final_validation_error"] = {
                "type": type(exc).__name__,
                "message": str(exc)[:2048],
            }
        if (
            not report["source_bindings_unchanged"]
            or scope["scoped_capture_override_restored"] is not True
            or scope["scoped_capture_override_identity_intact_before_restore"] is not True
            or "final_validation_error" in report
            or report["capture_evidence"] is None
        ):
            report["success"] = False
            report["native_exit_code"] = 1
            report["status"] = "failed_partial"
        try:
            _write_new(output / "input-report.json", report)
        except Exception as write_error:
            if pending_base_error is None:
                raise
            pending_base_error.add_note("Input report write also failed: " + str(write_error))
    if pending_base_error is not None:
        raise pending_base_error
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--model-sha256", required=True)
    parser.add_argument("--adapter-sha256", required=True)
    parser.add_argument("--adapter-config-sha256", required=True)
    parser.add_argument("--control", type=int, default=24580)
    parser.add_argument("--telem", type=int, default=24581)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--sensor-frames", type=int, default=8)
    parser.add_argument("--docker", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    report = run_input_capture(
        args.source,
        args.evidence,
        args.outdir,
        prompt=args.prompt,
        model_sha256=args.model_sha256,
        adapter_sha256=args.adapter_sha256,
        adapter_config_sha256=args.adapter_config_sha256,
        execute=args.execute,
        control_port=args.control,
        telemetry_port=args.telem,
        timeout_s=args.timeout,
        sensor_frames=args.sensor_frames,
        docker=args.docker,
    )
    print(
        json.dumps(
            {
                "input_report": _record(args.outdir / "input-report.json"),
                "success": report["success"],
                "native_exit_code": report["native_exit_code"],
            }
        )
    )
    return report["native_exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
