"""CPU-only byte/tensor audit of the single retained live-contract replay.

Rebuilds the original request and official processor inputs, never weights or
generation. An audit pass does not turn retained clocks into live clocks, repair
a rejected answer, promote a candidate, or establish flight success.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _path in (_ROOT, _ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from multidetect.autonomy.onboard_live_vlm_contract import (  # noqa: E402
    LIVE_REQUEST_CONTRACT,
    serialize_onboard_live_vlm_messages,
)
from multidetect.autonomy.onboard_vlm_runtime_contract import (  # noqa: E402
    parse_onboard_vlm_runtime_response,
)
from multidetect.autonomy.serialization import (  # noqa: E402
    canonical_json,
    canonical_sha256,
    require_int,
    require_real,
    require_sha256,
    strict_json_loads,
)
from scripts.run_onboard_live_vlm_retained_replay import (  # noqa: E402
    PINNED_REVISION,
    prepare_retained_request,
)

_FALSE_FLAGS = (
    "training_performed",
    "adapter_loaded",
    "mission_compiled",
    "transport_created",
    "execution_authority",
    "flight_verified",
    "current_gazebo_verified",
    "physical_performance_claimed",
    "hil_real_evidence",
    "jetson_evidence",
    "candidate_promoted",
    "final_release_allowed",
    "live_clock_verified",
    "sensor_to_host_clock_conversion_claimed",
)


def file_record(path: Path) -> dict:
    path = path.resolve(strict=True)
    digest, length = hashlib.sha256(), 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            length += len(block)
            digest.update(block)
    if path.stat().st_size != length:
        raise ValueError("file size changed during audit: " + str(path))
    return {"path": str(path), "bytes": length, "sha256": digest.hexdigest()}


def verify_records(records: list[dict]) -> dict[str, dict]:
    if not isinstance(records, list) or not records:
        raise ValueError("file bindings must be a nonempty list")
    bound = {}
    for item in records:
        if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
            raise ValueError("unexpected file binding fields")
        if not isinstance(item["path"], str) or not Path(item["path"]).is_absolute():
            raise ValueError("file binding path must be absolute")
        require_int(item["bytes"], "file bytes", minimum=0)
        require_sha256(item["sha256"], "file sha256")
        actual = file_record(Path(item["path"]))
        if actual != item or item["path"] in bound:
            raise ValueError("file binding changed or duplicated: " + item["path"])
        bound[item["path"]] = item
    return bound


def _read_json(path: Path) -> dict:
    value = strict_json_loads(path.read_bytes(), name=path.name)
    if not isinstance(value, dict):
        raise ValueError("audit JSON must be an object")
    canonical_json(value)
    return value


def message_binding(messages: list[dict]) -> dict:
    """Independently count and hash the PIL bytes sent to the processor."""
    result = []
    for message in messages:
        content = []
        for item in message["content"]:
            if item["type"] == "text":
                content.append({"type": "text", "text": item["text"]})
            elif item["type"] == "image":
                image = item["image"]
                if image.mode != "RGB":
                    raise ValueError("processor messages require actual RGB")
                content.append(
                    {
                        "type": "image",
                        "mode": image.mode,
                        "width": image.width,
                        "height": image.height,
                        "processed_rgb_sha256": hashlib.sha256(image.tobytes()).hexdigest(),
                    }
                )
            else:
                raise ValueError("unsupported processor message item")
        result.append({"role": message["role"], "content": content})
    return {"messages": result}


def tensor_records(inputs) -> dict:
    import torch

    records = {}
    for name, tensor in inputs.items():
        if isinstance(tensor, torch.Tensor):
            if tensor.device.type != "cpu":
                raise ValueError("audit must never move tensors to CUDA")
            records[name] = {
                "shape": list(tensor.shape),
                "dtype": str(tensor.dtype),
                "sha256": hashlib.sha256(
                    tensor.detach().contiguous().view(torch.uint8).numpy().tobytes()
                ).hexdigest(),
            }
    if not {"input_ids", "pixel_values", "image_grid_thw"} <= set(records):
        raise ValueError("actual processor omitted required inputs")
    return records


def audit_processor(request, metrics: dict, processor, raw: str, *, budget: dict) -> dict:
    messages = serialize_onboard_live_vlm_messages(request)
    binding = message_binding(messages)
    if canonical_sha256(binding) != metrics["actual_processor_messages_sha256"]:
        raise ValueError("actual processor messages differ")
    images = [
        item
        for message in binding["messages"]
        for item in message["content"]
        if item["type"] == "image"
    ]
    if images != metrics["processed_images"]:
        raise ValueError("processed RGB image bytes/geometry differ")
    patch = require_int(processor.image_processor.patch_size, "patch_size", minimum=1)
    merge = require_int(processor.image_processor.merge_size, "merge_size", minimum=1)
    kwargs = {
        "min_pixels": (patch * merge) ** 2,
        "max_pixels": request.max_total_pixels // len(images),
    }
    kwargs_before_call = dict(kwargs)
    inputs = processor.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=True,
        return_dict=True,
        return_tensors="pt",
        images_kwargs=kwargs,
    )
    # The official processor may consume this dict in-place. The frozen native
    # backend saved the very same post-call object: do not replace {} with a
    # fictional native pre-call receipt, or skip the exact observed comparison.
    if kwargs != metrics["processor_images_kwargs"]:
        raise ValueError("processor post-call kwargs differ from recorded native observation")
    records = tensor_records(inputs)
    if records != metrics["input_tensors"]:
        raise ValueError("actual processor tensor SHA/shape/dtype differ")
    token_count = require_int(metrics["input_tokens"], "input_tokens", minimum=1)
    max_input = require_int(budget["max_input_tokens"], "max_input_tokens", minimum=256)
    max_new = require_int(budget["max_new_tokens"], "max_new_tokens", minimum=1, maximum=1024)
    if tuple(inputs["input_ids"].shape) != (1, token_count) or token_count > max_input:
        raise ValueError("actual input token count/budget differs")
    grid = inputs["image_grid_thw"].tolist()
    if len(grid) != len(images) or any(len(row) != 3 or row[0] != 1 for row in grid):
        raise ValueError("static RGB grid geometry differs")
    pixels = [
        require_int(row[1], "grid h", minimum=1)
        * require_int(row[2], "grid w", minimum=1)
        * patch**2
        for row in grid
    ]
    expected_pixels = {
        "actual_image_grid_thw": grid,
        "processor_patch_size": patch,
        "actual_processor_pixels_per_image": pixels,
        "actual_processor_total_pixels": sum(pixels),
        "maximum_total_processor_pixels": request.max_total_pixels,
        "actual_processor_pixel_budget_verified": sum(pixels) <= request.max_total_pixels,
    }
    if (
        any(metrics[key] != value for key, value in expected_pixels.items())
        or not (expected_pixels["actual_processor_pixel_budget_verified"])
    ):
        raise ValueError("actual image grid/pixel budget differs")
    ids = metrics["output_token_ids"]
    if not isinstance(ids, list) or not ids:
        raise ValueError("native token IDs must be a nonempty list")
    for token in ids:
        require_int(token, "native output token ID", minimum=0)
    if (
        len(ids) > max_new
        or metrics["generated_tokens"] != len(ids)
        or (metrics["max_new_tokens_reached"] is not (len(ids) >= max_new))
    ):
        raise ValueError("native output token budget/count differs")
    decoded = processor.tokenizer.decode(
        ids, skip_special_tokens=True, clean_up_tokenization_spaces=False
    )
    if decoded != raw:
        raise ValueError("native token decode differs from unaltered raw UTF-8")
    return {
        "processor_messages": binding,
        "processor_messages_sha256": canonical_sha256(binding),
        "input_tensors": records,
        **expected_pixels,
        "input_tokens": token_count,
        "generated_tokens": len(ids),
        "token_decode_equals_exact_raw": True,
        "processor_images_kwargs_reconstructed_before_call": kwargs_before_call,
        "processor_images_kwargs_observed_after_call": dict(kwargs),
        "native_processor_kwargs_were_recorded_after_call": True,
    }


def check_report(report: dict) -> None:
    if report.get("contract") != "multidetect.onboard-live-vlm-retained-replay-v1":
        raise ValueError("unexpected replay report contract")
    require_int(report.get("schema_version"), "schema_version", minimum=1, maximum=1)
    require_int(report.get("native_exit_code"), "native_exit_code", minimum=0, maximum=1)
    if any(report.get(name) is not False for name in _FALSE_FLAGS):
        raise ValueError("replay report upgrades a training/live/flight/authority claim")
    for name in (
        "execute_requested",
        "input_prepared",
        "inference_submitted",
        "native_generation_observed",
        "vlm_inference_performed",
        "worker_created",
        "worker_closed",
        "source_records_unchanged",
        "original_capture_timestamps_preserved",
        "source_ns_preserved",
    ):
        if report.get(name) is not True:
            raise ValueError("replay lacks actual completed native attempt: " + name)
    if report.get("source_changes") != []:
        raise ValueError("replay recorded source drift")
    if report.get("model_revision") != PINNED_REVISION or (
        report.get("model_id") != "Qwen/Qwen3-VL-2B-Instruct"
    ):
        raise ValueError("replay is not the pinned official Qwen base")
    if report.get("mode") != "retained_sensor_replay_not_live_clock_or_current_Gazebo":
        raise ValueError("retained replay clock disclosure differs")
    if type(report.get("strict_intent_accepted")) is not bool:
        raise ValueError("strict acceptance must be an actual boolean")
    if report["native_exit_code"] == 0 and (
        report["errors"] or not report["strict_intent_accepted"]
    ):
        raise ValueError("native exit0 disagrees with strict intent/cleanup result")


def audit(args, *, processor_loader=None, enforce_d_drive=True) -> int:
    output = args.output_directory.resolve()
    if output.exists():
        raise FileExistsError("CPU audit output must be new; refusing overwrite")
    if enforce_d_drive and output.drive.upper() != "D:":
        raise ValueError("CPU audit output must be on D:")
    replay = args.replay_directory.resolve(strict=True)
    report_path = replay / "report.json"
    report_record = file_record(report_path)
    if report_record["sha256"] != require_sha256(args.report_sha256, "report_sha256"):
        raise ValueError("replay report SHA differs from expected terminal report")
    output.mkdir(parents=True, exist_ok=False)
    receipt = {
        "schema_version": 1,
        "contract": "multidetect.onboard-live-vlm-retained-replay-cpu-audit-v1",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "replay_report": report_record,
        "audit_verified": False,
        "model_weights_loaded": False,
        "gpu_used": False,
        "generation_repeated": False,
        "original_native_exit_code": None,
        "original_strict_intent_accepted": None,
        **dict.fromkeys(_FALSE_FLAGS, False),
        "errors": [],
    }
    bound_records = [report_record, file_record(Path(__file__))]
    try:
        report = _read_json(report_path)
        check_report(report)
        receipt["original_native_exit_code"] = report["native_exit_code"]
        receipt["original_strict_intent_accepted"] = report["strict_intent_accepted"]
        sources = verify_records(report["source_records"])
        outputs = verify_records(report["output_records"])
        if any(Path(path).parent != replay for path in outputs):
            raise ValueError("output binding escapes the replay directory")
        bound_records += list(sources.values()) + list(outputs.values())
        required = {"prepared-input.json", "native-attempt.json", "raw-output.txt"}
        if not required <= {Path(path).name for path in outputs}:
            raise ValueError("native prepared input/attempt/raw files are not bound")
        prepared = _read_json(replay / "prepared-input.json")
        model_input = prepared["model_input"]
        manifest = args.manifest.resolve(strict=True)
        if str(manifest) not in sources:
            raise ValueError("original frame-set manifest is not source-bound")
        runtime = prepared["runtime_request"]
        deadline = require_real(model_input["deadline_at_s"], "deadline_at_s") - require_real(
            model_input["requested_at_s"], "requested_at_s"
        )
        request, frame_records = prepare_retained_request(
            manifest,
            sources[str(manifest)]["sha256"],
            repository_root=args.repository_root,
            prompt_text=model_input["prompt_original_text"],
            prompt_issued_at_utc=model_input["prompt"]["issued_at"],
            deadline_s=deadline,
            max_total_pixels=runtime["max_total_pixels"],
        )
        if any(sources.get(item["path"]) != item for item in frame_records):
            raise ValueError("original source frame bytes are not report-bound")
        if prepared != {
            "model_input": request.model_input.to_mapping(),
            "runtime_request": request.to_mapping(),
            "input_sha256": request.input_sha256,
            "request_sha256": request.canonical_hash,
            "actual_rgb_bytes": sum(len(f.image_bytes) for f in request.model_input.frames),
            "execution_authority": False,
        } or (
            report["input_sha256"] != request.input_sha256
            or report["request_sha256"] != request.canonical_hash
            or report["scope_clock_epoch"] != request.model_input.scope.clock_epoch
        ):
            raise ValueError("reconstructed request differs from native prepared input")
        attempt = _read_json(replay / "native-attempt.json")
        raw = (replay / "raw-output.txt").read_bytes().decode("utf-8")
        if report["native_attempt"] != attempt or attempt["raw_output"] != raw:
            raise ValueError("original native attempt/raw file/report differ")
        latency = require_real(attempt["inference_latency_ms"], "host inference latency", minimum=0)
        completed = require_real(attempt["completed_at_s"], "replay completion time", minimum=0)
        if abs(completed - (request.model_input.requested_at_s + latency / 1000)) > 1e-6:
            raise ValueError("worker elapsed time is not preserved in the replay clock domain")
        for key, value in {
            "generation": report["generation"],
            "request_sha256": request.canonical_hash,
            "input_sha256": request.input_sha256,
            "scope_sha256": request.model_input.scope.canonical_hash,
            "execution_authority": False,
        }.items():
            actual = attempt.get(key)
            if actual is not value if type(value) is bool else actual != value:
                raise ValueError("native attempt identity differs: " + key)
        require_int(attempt["generation"], "generation", minimum=0)
        metrics = attempt["backend_metrics"]
        for key, value in {
            "backend": "native_pinned_qwen3_vl_cuda_live_partial_state_v1",
            "request_contract": LIVE_REQUEST_CONTRACT,
            "request_sha256": request.canonical_hash,
            "input_sha256": request.input_sha256,
            "model_sha256": report["model_sha256"],
            "adapter_loaded": False,
            "execution_authority": False,
            "partial_navigation_context_only": True,
            "processed_images_role": "serializer_output_before_processor_smart_resize",
            "factual_sensor_state_sha256": canonical_sha256(
                {"factual_sensor_state": request.model_input.to_mapping()["factual_sensor_state"]}
            ),
        }.items():
            actual = metrics.get(key)
            if actual is not value if type(value) is bool else actual != value:
                raise ValueError("native backend identity differs: " + key)
        snapshot = args.snapshot.resolve(strict=True)
        if (
            snapshot.name != PINNED_REVISION
            or sources.get(str(snapshot / "model.safetensors"), {}).get("sha256")
            != report["model_sha256"]
        ):
            raise ValueError("audit snapshot weights identity differs")
        for path in snapshot.iterdir():
            if path.is_file() and path.suffix in {".json", ".txt"} and str(path) not in sources:
                raise ValueError("processor metadata not in original source binding")
        for package, expected in report["runtime"]["package_versions"].items():
            if importlib.metadata.version(package) != expected:
                raise ValueError("audit processor runtime version differs: " + package)
        if processor_loader is None:
            from transformers import AutoProcessor

            processor_loader = AutoProcessor.from_pretrained
        processor = processor_loader(str(snapshot), local_files_only=True, trust_remote_code=False)
        receipt["processor_audit"] = audit_processor(
            request, metrics, processor, raw, budget=report["generation_budget"]
        )
        try:
            response = parse_onboard_vlm_runtime_response(
                raw,
                request,
                current_scope=request.model_input.scope,
                completed_at_s=attempt["completed_at_s"],
                inference_latency_ms=attempt["inference_latency_ms"],
            )
        except Exception as exc:
            if report["strict_intent_accepted"] or report["result"] is not None:
                raise ValueError("original accepted result fails exact strict reparse") from exc
            receipt["strict_rejection_preserved"] = type(exc).__name__ + ": " + str(exc)
        else:
            result = report["result"]
            if (
                not report["strict_intent_accepted"]
                or not isinstance(result, dict)
                or (
                    result["response"] != response.to_mapping()
                    or result["raw_output"] != raw
                    or result["generation"] != attempt["generation"]
                    or result["request_sha256"] != attempt["request_sha256"]
                    or result["backend_metrics"] != metrics
                    or result["completed_at_s"] != attempt["completed_at_s"]
                    or result["inference_latency_ms"] != attempt["inference_latency_ms"]
                )
            ):
                raise ValueError("strict reparse disagrees with original worker result")
        source_run = report["original_source_run"]
        if source_run is not None:
            if not isinstance(source_run, dict) or source_run.get("path") not in sources:
                raise ValueError("original source run is not actually file-bound")
            source_document = _read_json(Path(source_run["path"]))
            base = source_document.get("base_docker_acceptance", {})
            expected_run = {
                "path": source_run["path"],
                "status": source_document.get("status"),
                "base_docker_acceptance_gate_passed": base.get("gate_passed")
                if isinstance(base, dict)
                else None,
                "complete_autonomous_task_success_claimed": False,
            }
            if source_run != expected_run:
                raise ValueError("original failed/successful source run facts were changed")
        receipt["original_source_run"] = source_run
        receipt["input_sha256"] = request.input_sha256
        receipt["request_sha256"] = request.canonical_hash
        receipt["original_capture_timestamps_preserved"] = True
        receipt["source_ns_preserved"] = True
        receipt["audit_verified"] = True
    except Exception as exc:
        receipt["errors"].append(type(exc).__name__ + ": " + str(exc))
    # Reopen before sealing; do not silently turn an audit-time change into a pass.
    for record in bound_records:
        try:
            if file_record(Path(record["path"])) != record:
                raise ValueError("bytes changed during CPU audit")
        except Exception as exc:
            receipt["errors"].append(record["path"] + ": " + type(exc).__name__ + ": " + str(exc))
    code = 0 if receipt["audit_verified"] and not receipt["errors"] else 1
    receipt["audit_verified"] = code == 0
    receipt["native_exit_code"] = code
    receipt["verified_records"] = bound_records
    receipt["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    payload = (json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    with (output / "audit.json").open("xb") as stream:
        stream.write(payload)
    return code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--replay-directory", type=Path, required=True)
    parser.add_argument("--report-sha256", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    return audit(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
