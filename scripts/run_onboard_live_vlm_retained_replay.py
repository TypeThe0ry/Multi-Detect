"""Bounded replay of retained Gazebo wire-v3 RGB/partial state; never live flight.

Default: validate and prepare two original sensor frames, without a process/GPU.
--execute explicitly runs the pinned local base through the persistent worker.
Original host capture timestamps and simulator nanoseconds are never rewritten;
the worker projects measured elapsed onto a declared virtual retained clock.
This is not current Gazebo, live clock synchronization, action training or flight.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import NAMESPACE_URL, uuid5

_CODE_ROOT = Path(__file__).resolve().parents[1]
for _path in (_CODE_ROOT, _CODE_ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from multidetect.autonomy.contracts import PromptEnvelope  # noqa: E402
from multidetect.autonomy.onboard_live_vlm_backend import LiveQwenVlmBackendFactory  # noqa: E402
from multidetect.autonomy.onboard_live_vlm_contract import (  # noqa: E402
    OnboardLiveMultimodalInput,
    OnboardLiveVlmRequest,
    gazebo_sensor_fact_json,
)
from multidetect.autonomy.onboard_multimodal import (  # noqa: E402
    MissionEvidenceLedger,
    OnboardImageFrame,
    OnboardMultimodalScope,
)
from multidetect.autonomy.onboard_vlm_runtime_contract import (  # noqa: E402
    parse_onboard_vlm_runtime_response,
)
from multidetect.autonomy.onboard_vlm_worker import OnboardVlmProcessWorker  # noqa: E402
from multidetect.autonomy.serialization import (  # noqa: E402
    canonical_json,
    require_int,
    require_real,
    require_sha256,
    strict_json_loads,
    strict_object,
)
from multidetect.backends.gazebo_sim import (  # noqa: E402
    reconstruct_gazebo_sim_wire_frame,
    validate_frame_set_clock,
)

PINNED_REVISION = "89644892e4d85e24eaac8bacfd4f463576704203"
_SOURCE_PATHS = (
    Path(__file__),
    *(
        _CODE_ROOT / path
        for path in (
            "src/multidetect/autonomy/onboard_live_vlm_contract.py",
            "src/multidetect/autonomy/onboard_live_vlm_backend.py",
            "src/multidetect/autonomy/onboard_vlm_runtime_contract.py",
            "src/multidetect/autonomy/onboard_vlm_backend.py",
            "src/multidetect/autonomy/onboard_vlm_worker.py",
            "src/multidetect/autonomy/onboard_multimodal.py",
            "src/multidetect/autonomy/serialization.py",
            "src/multidetect/autonomy/contracts.py",
            "src/multidetect/backends/gazebo_sim.py",
            "src/multidetect/clock.py",
            "scripts/gazebo_depth_wire.py",
        )
    ),
)


def _record(path: Path) -> dict:
    resolved = path.resolve(strict=True)
    digest = hashlib.sha256()
    with resolved.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return {"path": str(resolved), "bytes": resolved.stat().st_size, "sha256": digest.hexdigest()}


def _write_json(path: Path, value: dict) -> None:
    # Serialize first so malformed results cannot create a partial evidence file.
    canonical_json(value)
    payload = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    with path.open("xb") as stream:
        stream.write(payload)


def prepare_retained_request(
    manifest: Path,
    manifest_sha256: str,
    *,
    repository_root: Path,
    prompt_text: str,
    prompt_issued_at_utc: str,
    deadline_s: float = 30.0,
    max_total_pixels: int = 200704,
) -> tuple[OnboardLiveVlmRequest, list[dict]]:
    """Reopen exactly indices 0/1; do not import scenario/model/label facts."""
    deadline = require_real(deadline_s, "deadline_s", minimum=0.05, maximum=120.0)
    expected = require_sha256(manifest_sha256, "manifest_sha256")
    root = repository_root.resolve(strict=True)
    path = manifest.resolve(strict=True)
    if not path.is_relative_to(root):
        raise ValueError("retained manifest must be inside the explicit repository")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("retained manifest actual bytes SHA-256 mismatch")
    document = strict_json_loads(raw, name="retained frame-set manifest")
    canonical_json(document)
    if not isinstance(document, dict):
        raise ValueError("retained manifest must be a JSON object")
    require_int(document.get("schema_version"), "schema_version", minimum=2, maximum=2)
    require_int(document.get("wire_version"), "wire_version", minimum=3, maximum=3)
    if (
        document.get("schema"),
        document.get("schema_version"),
        document.get("wire_version"),
        document.get("pixel_format"),
    ) != ("multidetect.gazebo-bgr24-frame-set", 2, 3, "BGR24"):
        raise ValueError("retained source must be the existing wire-v3 BGR frame-set schema")
    rows = document.get("frames")
    if (
        not isinstance(rows, list)
        or len(rows) < 2
        or require_int(document.get("frame_count"), "frame_count", minimum=2) != len(rows)
    ):
        raise ValueError("retained source requires original frame-set indices 0 and 1")
    validate_frame_set_clock(document)
    # This replay only claims preservation of the old host monotonic domain.
    if any(row["provenance"].get("captured_at_clock") != "host_monotonic" for row in rows[:2]):
        raise ValueError("this retained replay requires original host_monotonic provenance")
    identifier = str(uuid5(NAMESPACE_URL, "retained-gazebo-replay:" + expected + ":" + prompt_text))
    prompt = PromptEnvelope(
        identifier,
        prompt_text,
        "und",
        prompt_issued_at_utc,
        "retained-replay-caller",
        "retained-gazebo-sensor-replay",
    )
    scope = OnboardMultimodalScope(
        identifier,
        prompt.canonical_hash,
        prompt.aircraft_id,
        "retained-shadow-task",
        "retained-host-monotonic-replay:" + expected,
    )
    records = [{"path": str(path), "bytes": len(raw), "sha256": expected}]
    frames, facts = [], []
    previous_stamp = None
    required = frozenset(
        {
            "bytes",
            "file",
            "frame_id",
            "provenance",
            "sequence",
            "sha256",
            "wire_payload_sha256",
            "wire_record_sha256",
            "raw_bytes_sha256_matches_wire_payload",
        }
    )
    for index, row in enumerate(rows[:2]):
        strict_object(row, name="retained selected frame", required=required)
        if row["raw_bytes_sha256_matches_wire_payload"] is not True:
            raise ValueError("retained source must declare the original raw wire payload binding")
        if require_int(row["sequence"], "sequence", minimum=0) != index:
            raise ValueError("selected retained rows must preserve indices/sequences 0 and 1")
        name = row["file"]
        if (
            not isinstance(name, str)
            or PurePosixPath(name).parts != (name,)
            or name != f"frame-{index:06d}.bgr24"
        ):
            raise ValueError("retained frame path must be its safe original local filename")
        source = path.parent / name
        if source.is_symlink() or source.resolve(strict=True).parent != path.parent:
            raise ValueError("retained frame path escapes the source directory")
        bgr = source.read_bytes()
        digest = hashlib.sha256(bgr).hexdigest()
        provenance = row["provenance"]
        if len(bgr) != require_int(row["bytes"], "frame bytes", minimum=1) or not (
            digest == row["sha256"] == row["wire_payload_sha256"] == provenance["payload_sha256"]
        ):
            raise ValueError("original BGR source bytes/geometry/hash differ")
        wire = reconstruct_gazebo_sim_wire_frame(
            unsigned_header_hex=provenance["wire_unsigned_header_hex"],
            payload_bgr24=bgr,
            expected_sequence=index,
            expected_width=provenance["width"],
            expected_height=provenance["height"],
            previous_sensor_stamp_ns=previous_stamp,
        )
        if (
            not (wire.record_sha256 == row["wire_record_sha256"] == provenance["record_sha256"])
            or row["frame_id"] != provenance["frame_id"]
        ):
            raise ValueError("retained source row disagrees with original wire/provenance")
        rgb = bytearray(bgr)
        rgb[0::3], rgb[2::3] = bgr[2::3], bgr[0::3]
        frame = OnboardImageFrame(
            scope,
            row["frame_id"],
            provenance["captured_at_s"],
            bytes(rgb),
            hashlib.sha256(rgb).hexdigest(),
            wire.width,
            wire.height,
            "rgb8",
            None,
        )
        facts.append(gazebo_sensor_fact_json(frame, provenance))
        frames.append(frame)
        records.append({"path": str(source.resolve()), "bytes": len(bgr), "sha256": digest})
        previous_stamp = wire.sensor_stamp_ns
    frames = tuple(frames)
    requested = frames[-1].captured_at_s + 0.001
    model_input = OnboardLiveMultimodalInput(
        scope,
        prompt,
        frames,
        MissionEvidenceLedger(scope, frames),
        requested,
        requested + deadline,
        max_frame_age_s=deadline + requested - frames[0].captured_at_s,
        max_gap_s=5.0,
        prompt_text_original=prompt_text,
        factual_sensor_state_json=tuple(facts),
    )
    return OnboardLiveVlmRequest(model_input, max_total_pixels=max_total_pixels), records


def _source_changes(records: list[dict]) -> list[dict]:
    failures = []
    for record in records:
        try:
            actual = _record(Path(record["path"]))
            if actual != record:
                failures.append(
                    {"path": record["path"], "reason": "source_bytes_changed", "actual": actual}
                )
        except Exception as exc:
            failures.append(
                {"path": record["path"], "reason": type(exc).__name__ + ": " + str(exc)}
            )
    return failures


def run(args, *, worker_type=OnboardVlmProcessWorker, enforce_d_drive=True) -> int:
    output = args.output_directory.resolve()
    if output.exists():
        raise FileExistsError("retained replay output must be fresh; refusing overwrite")
    if enforce_d_drive and output.drive.upper() != "D:":
        raise ValueError("retained replay artifacts must stay on D:")
    request, records = prepare_retained_request(
        args.manifest,
        args.manifest_sha256,
        repository_root=args.repository_root,
        prompt_text=args.prompt,
        prompt_issued_at_utc=args.prompt_issued_at_utc,
        deadline_s=args.deadline_s,
        max_total_pixels=args.max_total_pixels,
    )
    snapshot = args.snapshot.resolve(strict=True)
    if snapshot.name != PINNED_REVISION:
        raise ValueError("retained replay requires the pinned cached Qwen revision; never download")
    factory = LiveQwenVlmBackendFactory(str(snapshot), args.model_sha256, 8192, args.max_new_tokens)
    model_record = _record(snapshot / "model.safetensors")
    if model_record["sha256"] != args.model_sha256:
        raise ValueError("pinned local base weights differ from requested model SHA-256")
    records += [_record(p) for p in _SOURCE_PATHS]
    records += [
        _record(p)
        for p in sorted(snapshot.iterdir())
        if p.is_file() and p.suffix in {".json", ".txt"}
    ]
    records.append(model_record)
    source_run = None
    if args.source_run_report is not None:
        run_path = args.source_run_report.resolve(strict=True)
        run_raw = run_path.read_bytes()
        run_document = strict_json_loads(run_raw, name="original source run report")
        canonical_json(run_document)
        base = run_document.get("base_docker_acceptance", {})
        source_run = {
            "path": str(run_path),
            "status": run_document.get("status"),
            "base_docker_acceptance_gate_passed": base.get("gate_passed")
            if isinstance(base, dict)
            else None,
            "complete_autonomous_task_success_claimed": False,
        }
        records.append(
            {
                "path": str(run_path),
                "bytes": len(run_raw),
                "sha256": hashlib.sha256(run_raw).hexdigest(),
            }
        )
    output.mkdir(parents=True, exist_ok=False)
    versions = {}
    for package in ("torch", "transformers", "Pillow", "numpy"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    report = {
        "schema_version": 1,
        "contract": "multidetect.onboard-live-vlm-retained-replay-v1",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "retained_sensor_replay_not_live_clock_or_current_Gazebo",
        "execute_requested": bool(args.execute),
        "model_id": "Qwen/Qwen3-VL-2B-Instruct",
        "model_revision": PINNED_REVISION,
        "model_sha256": args.model_sha256,
        "generation_budget": {"max_input_tokens": 8192, "max_new_tokens": args.max_new_tokens},
        "source_records": records,
        "input_prepared": True,
        "runtime": {
            "python": sys.version,
            "executable": sys.executable,
            "platform": sys.platform,
            "package_versions": versions,
        },
        "original_source_run": source_run,
        "input_sha256": request.input_sha256,
        "request_sha256": request.canonical_hash,
        "scope_clock_epoch": request.model_input.scope.clock_epoch,
        "source_frame_indices": [0, 1],
        "original_capture_timestamps_preserved": True,
        "source_ns_preserved": True,
        "sensor_to_host_clock_conversion_claimed": False,
        "live_clock_verified": False,
        "completion_time_semantics": (
            "worker_measured_elapsed_projected_only_into_explicit_retained_replay_domain"
        ),
        "inference_submitted": False,
        "vlm_inference_performed": False,
        "native_generation_observed": False,
        "strict_intent_accepted": False,
        "native_attempt": None,
        "result": None,
        "worker_created": False,
        "worker_closed": None,
        "errors": [],
        "training_performed": False,
        "adapter_loaded": False,
        "mission_compiled": False,
        "transport_created": False,
        "execution_authority": False,
        "flight_verified": False,
        "current_gazebo_verified": False,
        "physical_performance_claimed": False,
        "hil_real_evidence": False,
        "jetson_evidence": False,
        "candidate_promoted": False,
        "final_release_allowed": False,
    }
    worker = None
    try:
        _write_json(
            output / "prepared-input.json",
            {
                "model_input": request.model_input.to_mapping(),
                "runtime_request": request.to_mapping(),
                "input_sha256": request.input_sha256,
                "request_sha256": request.canonical_hash,
                "actual_rgb_bytes": sum(
                    len(frame.image_bytes) for frame in request.model_input.frames
                ),
                "execution_authority": False,
            },
        )
        if args.execute:
            worker = worker_type(factory, startup_timeout_s=120.0)
            report["worker_created"] = True
            worker.start()
            began = time.monotonic()
            while worker.state == "starting" and time.monotonic() - began <= 122.0:
                time.sleep(0.02)
            report["startup_elapsed_s"] = time.monotonic() - began
            if worker.state != "idle":
                raise RuntimeError("worker startup failed: " + str(worker.error or worker.state))
            report["worker_pid"] = worker.process_id
            generation = worker.submit(request, observed_now_s=request.model_input.requested_at_s)
            report["generation"] = generation
            report["inference_submitted"] = True
            began = time.monotonic()
            while worker.state == "pending" and time.monotonic() - began <= args.deadline_s + 2.0:
                time.sleep(0.01)
            report["worker_terminal_state"] = worker.state
            report["worker_error"] = worker.error
            result = worker.take_result()
            attempt = worker.last_attempt
            report["native_attempt"] = attempt
            if attempt is not None:
                report["native_generation_observed"] = True
                report["vlm_inference_performed"] = True
                _write_json(output / "native-attempt.json", attempt)
                with (output / "raw-output.txt").open("xb") as stream:
                    stream.write(attempt["raw_output"].encode("utf-8"))
            # Reparse original bytes; result objects or an enum alone are not approval.
            if result is not None:
                if (
                    result.generation != generation
                    or result.request_sha256 != request.canonical_hash
                    or attempt is None
                    or result.raw_output != attempt["raw_output"]
                ):
                    raise ValueError("worker returned a different generation/request/raw identity")
                response = parse_onboard_vlm_runtime_response(
                    result.raw_output,
                    request,
                    current_scope=request.model_input.scope,
                    completed_at_s=result.completed_at_s,
                    inference_latency_ms=result.inference_latency_ms,
                )
                if response.to_mapping() != result.response.to_mapping():
                    raise ValueError("worker typed response differs from exact original reparse")
                report["result"] = result.to_mapping()
                report["strict_intent_accepted"] = True
            else:
                report["errors"].append(
                    {"phase": "generation_or_strict_parse", "error": worker.error or worker.state}
                )
            report["backend_failure_details_available"] = attempt is not None
    except Exception as exc:
        report["errors"].append({"phase": "run", "error": type(exc).__name__ + ": " + str(exc)})
    finally:
        if worker is not None:
            try:
                worker.close()
                report["worker_closed"] = worker.state == "closed"
                if not report["worker_closed"]:
                    report["errors"].append({"phase": "cleanup", "error": "worker_not_closed"})
            except Exception as exc:
                report["worker_closed"] = False
                report["errors"].append(
                    {"phase": "cleanup", "error": type(exc).__name__ + ": " + str(exc)}
                )
        report["source_changes"] = _source_changes(records)
        report["source_records_unchanged"] = not report["source_changes"]
        output_records = []
        try:
            output_files = sorted(output.iterdir())
        except Exception as exc:
            output_files = []
            report["errors"].append(
                {"phase": "output_reopen", "error": type(exc).__name__ + ": " + str(exc)}
            )
        for path in output_files:
            try:
                if path.is_file():
                    output_records.append(_record(path))
            except Exception as exc:
                report["errors"].append(
                    {
                        "phase": "output_reopen",
                        "path": str(path),
                        "error": type(exc).__name__ + ": " + str(exc),
                    }
                )
        code = (
            0
            if not report["errors"]
            and report["source_records_unchanged"]
            and (not args.execute or report["strict_intent_accepted"])
            else 1
        )
        report["native_exit_code"] = code
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        report["output_records"] = output_records
        _write_json(output / "report.json", report)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--source-run-report", type=Path)
    parser.add_argument("--model-sha256", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--prompt-issued-at-utc", default=datetime.now(timezone.utc).isoformat())
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--deadline-s", type=float, default=30.0)
    parser.add_argument("--max-total-pixels", type=int, default=200704)
    parser.add_argument("--max-new-tokens", type=int, default=384)
    parser.add_argument("--execute", action="store_true")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
