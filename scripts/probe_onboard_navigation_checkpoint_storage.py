"""Create-only CPU checkpoint-storage diagnostic, never a model/training loader.

The standard-library controller survives native worker crashes. Only the pinned
Qwen checkpoint is admitted, and the worker returns its first eight BF16 values.
CPU pread in safetensors 0.8.0 may still read the whole first tensor (622 MB).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import struct
import subprocess
import sys
from pathlib import Path

CONTRACT = "multidetect.onboard-navigation-checkpoint-storage-probe-v1"
WORKSPACE = Path(__file__).resolve().parents[1]
CHECKPOINT_BYTES = 4255140312
CHECKPOINT_SHA256 = "7de1838c87a5349b016c26a1c3f7d2bc400a3d485f95ef39a7059ffd734977a0"
HEADER_BYTES = 76240
HEADER_SHA256 = "a4c22c14e0d987dd572a283e92643374babeccc2f86cf2f8020e6144389c3f4d"
TENSOR_NAME = "model.language_model.embed_tokens.weight"
TENSOR_INFO = {"dtype": "BF16", "shape": [151936, 2048], "data_offsets": [0, 622329856]}
SLICE_SHA256 = "7204697528fb6af2315f57cfd6878937abfc056dc8682b513741bb3dd1358e22"
DEFAULT_CHECKPOINT = WORKSPACE / (
    "artifacts/cache/huggingface-vlm/models--Qwen--Qwen3-VL-2B-Instruct/"
    "snapshots/89644892e4d85e24eaac8bacfd4f463576704203/model.safetensors"
)
CLAIMS = {
    "gpu_used": False,
    "optimizer_created": False,
    "model_loaded": False,
    "quality_claim": False,
    "candidate_promoted": False,
    "physical_performance_claimed": False,
    "hil_real_evidence": False,
    "final_release_allowed": False,
}


def strict_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def reject(value):
        raise ValueError(f"Non-finite JSON constant: {value}")

    value = json.loads(raw, object_pairs_hook=unique, parse_constant=reject)

    def finite(nested):
        if isinstance(nested, float) and not math.isfinite(nested):
            raise ValueError("Non-finite JSON exponent")
        if isinstance(nested, dict):
            for item in nested.values():
                finite(item)
        elif isinstance(nested, list):
            for item in nested:
                finite(item)

    finite(value)
    return value


def file_record(path):
    path = Path(path).resolve(strict=True)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def create_json(path, value):
    with Path(path).open("xb") as stream:
        stream.write(json.dumps(value, allow_nan=False, sort_keys=True).encode() + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def inspect_header(path):
    """Only stdlib reads: exact header plus independently sourced 16 gold bytes."""
    with Path(path).open("rb") as stream:
        prefix = stream.read(8)
        if len(prefix) != 8 or struct.unpack("<Q", prefix)[0] != HEADER_BYTES:
            raise ValueError("Pinned checkpoint header length mismatch")
        raw = stream.read(HEADER_BYTES)
        if len(raw) != HEADER_BYTES or hashlib.sha256(raw).hexdigest() != HEADER_SHA256:
            raise ValueError("Pinned checkpoint header hash mismatch")
        header = strict_json(raw)
        names = [name for name in header if name != "__metadata__"]
        if not names or names[0] != TENSOR_NAME or header[TENSOR_NAME] != TENSOR_INFO:
            raise ValueError("Pinned first tensor identity mismatch")
        stream.seek(8 + HEADER_BYTES + TENSOR_INFO["data_offsets"][0])
        first_eight = stream.read(16)
    if len(first_eight) != 16 or hashlib.sha256(first_eight).hexdigest() != SLICE_SHA256:
        raise ValueError("Pinned first eight values mismatch")
    return {
        "header_bytes": HEADER_BYTES,
        "header_sha256": HEADER_SHA256,
        "tensor_name": TENSOR_NAME,
        "tensor_info": TENSOR_INFO,
        "output_elements": 8,
        "output_bytes": 16,
        "slice_sha256": SLICE_SHA256,
        "tensor_storage_bytes": TENSOR_INFO["data_offsets"][1] - TENSOR_INFO["data_offsets"][0],
        "cpu_pread_may_read_whole_tensor": True,
        "initial_header_mmap_not_claimed_avoided": True,
    }


def verify_checkpoint(path):
    record = file_record(path)
    if record["bytes"] != CHECKPOINT_BYTES or record["sha256"] != CHECKPOINT_SHA256:
        raise ValueError("Only the exact pinned original checkpoint is admitted")
    return record, inspect_header(path)


def emit(value):
    print(json.dumps(value, allow_nan=False, sort_keys=True), flush=True)


def worker(args):
    emit(
        {
            "event": "worker_started",
            "phase": args.worker_phase,
            "pid": os.getpid(),
            "parent_pid": os.getppid(),
            "executable": sys.executable,
        }
    )
    if os.environ.get("CUDA_VISIBLE_DEVICES") != "":
        raise ValueError("Worker must have all CUDA devices masked")
    source = file_record(__file__)
    if source["sha256"] != args.source_sha256:
        raise ValueError("Probe source changed before worker admission")
    header_before = inspect_header(args.checkpoint)
    version = importlib.metadata.version("safetensors")
    if version != args.expected_safetensors_version:
        raise ValueError("Actual safetensors version differs from declared comparison")
    emit(
        {
            "event": "before_dependency_import",
            "safetensors_version": version,
            "torch_distribution_version": importlib.metadata.version("torch"),
            "backend": args.backend,
        }
    )
    import safetensors
    import torch

    if safetensors.__version__ != version:
        raise ValueError("Imported safetensors does not match distribution metadata")
    kwargs = {"framework": "pt", "device": "cpu"}
    if args.backend != "default":
        kwargs["backend"] = args.backend
    emit(
        {
            "event": "before_safe_open",
            "phase": args.worker_phase,
            "safetensors_path": safetensors.__file__,
            "torch_path": torch.__file__,
            "torch_version": torch.__version__,
            "cpu_only": True,
        }
    )
    with safetensors.safe_open(str(args.checkpoint), **kwargs) as reader:
        tensor_slice = reader.get_slice(TENSOR_NAME)
        if list(tensor_slice.get_shape()) != TENSOR_INFO["shape"]:
            raise ValueError("Reader first tensor shape mismatch")
        if tensor_slice.get_dtype() != TENSOR_INFO["dtype"]:
            raise ValueError("Reader first tensor dtype mismatch")
        emit(
            {
                "event": "reader_metadata_verified",
                "phase": args.worker_phase,
                "tensor_name": TENSOR_NAME,
                "shape": TENSOR_INFO["shape"],
                "dtype": "BF16",
            }
        )
        if args.worker_phase == "slice":
            emit({"event": "before_first_eight_cpu_slice", **header_before})
            tensor = tensor_slice[0:1, 0:8]
            if tensor.device.type != "cpu" or list(tensor.shape) != [1, 8]:
                raise ValueError("Result is not exactly eight CPU elements")
            if tensor.dtype != torch.bfloat16 or tensor.numel() != 8:
                raise ValueError("Result dtype/element bound mismatch")
            raw = tensor.detach().contiguous().view(torch.uint8).numpy().tobytes()
            if len(raw) != 16 or hashlib.sha256(raw).hexdigest() != SLICE_SHA256:
                raise ValueError("Native reader values differ from original 16-byte gold")
            emit(
                {
                    "event": "first_eight_cpu_slice_verified",
                    "phase": args.worker_phase,
                    "slice_sha256": SLICE_SHA256,
                    "elements": 8,
                    "bytes": 16,
                    "values": tensor.float().tolist(),
                }
            )
    if inspect_header(args.checkpoint) != header_before or file_record(__file__) != source:
        raise ValueError("Worker header or probe source changed")
    emit(
        {
            "event": "worker_completed",
            "phase": args.worker_phase,
            "source_and_header_verified": True,
            **CLAIMS,
        }
    )
    return 0


def child_command(args, phase):
    return [
        str(args.python),
        "-u",
        "-B",
        "-X",
        "utf8",
        "-X",
        "faulthandler",
        str(Path(__file__).resolve()),
        "--worker-phase",
        phase,
        "--checkpoint",
        str(args.checkpoint),
        "--source-sha256",
        args.source_sha256,
        "--backend",
        args.backend,
        "--expected-safetensors-version",
        args.expected_safetensors_version,
    ]


def read_worker_events(path):
    events = []
    for line in Path(path).read_bytes().splitlines():
        try:
            event = strict_json(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def phase_events_verified(events, phase):
    names = ("worker_started", "reader_metadata_verified", "worker_completed")
    selected = {name: [event for event in events if event.get("event") == name] for name in names}
    if any(len(items) != 1 or items[0].get("phase") != phase for items in selected.values()):
        return False
    start = selected["worker_started"][0]
    metadata = selected["reader_metadata_verified"][0]
    finish = selected["worker_completed"][0]
    if any(type(start.get(key)) is not int or start[key] <= 0 for key in ("pid", "parent_pid")):
        return False
    if (
        metadata.get("tensor_name") != TENSOR_NAME
        or metadata.get("shape") != TENSOR_INFO["shape"]
        or metadata.get("dtype") != "BF16"
        or finish.get("source_and_header_verified") is not True
        or any(finish.get(key) is not False for key in CLAIMS)
    ):
        return False
    order = [events.index(selected[name][0]) for name in names]
    if order != sorted(order):
        return False
    slices = [event for event in events if event.get("event") == "first_eight_cpu_slice_verified"]
    if phase == "metadata":
        return not slices
    if len(slices) != 1:
        return False
    verified = slices[0]
    return (
        verified.get("phase") == phase
        and verified.get("slice_sha256") == SLICE_SHA256
        and type(verified.get("elements")) is int
        and verified["elements"] == 8
        and type(verified.get("bytes")) is int
        and verified["bytes"] == 16
        and order[1] < events.index(verified) < order[2]
    )


def run_phase(args, phase, source_before):
    output = args.output_dir / phase
    output.mkdir()
    command = child_command(args, phase)
    environment = os.environ.copy()
    environment.update(
        {
            "CUDA_VISIBLE_DEVICES": "",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    with (output / "stdout.bin").open("xb") as stdout, (output / "stderr.bin").open("xb") as stderr:
        process = subprocess.Popen(
            command, stdout=stdout, stderr=stderr, env=environment, cwd=WORKSPACE, shell=False
        )
        launch_error = None
        timed_out = False
        try:
            create_json(
                output / "launch.json",
                {
                    "argv": command,
                    "launcher_pid": process.pid,
                    "controller_pid": os.getpid(),
                    "phase": phase,
                    "cuda_visible_devices": "",
                    "source_before": source_before,
                },
            )
        except Exception as exc:
            launch_error = {"type": type(exc).__name__, "message": str(exc)}
        finally:
            # Even launch-record failure must reap our owned process before closing streams.
            try:
                returncode = process.wait(timeout=args.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                returncode = process.wait()
        stdout.flush()
        stderr.flush()
        os.fsync(stdout.fileno())
        os.fsync(stderr.fileno())
    events = read_worker_events(output / "stdout.bin")
    starts = [event for event in events if event.get("event") == "worker_started"]
    receipts_verified = phase_events_verified(events, phase)
    record = {
        "phase": phase,
        "launcher_pid": process.pid,
        "returncode": returncode,
        "windows_unsigned_returncode": returncode & 0xFFFFFFFF,
        "timed_out": timed_out,
        "launch_record_error": launch_error,
        "phase_events_verified": receipts_verified,
        "worker_pid": starts[0].get("pid") if len(starts) == 1 else None,
        "worker_parent_pid": starts[0].get("parent_pid") if len(starts) == 1 else None,
        "worker_pid_basis": "raw child stdout; not independently verified by OS",
        "worker_exit_unknown_if_launcher_timeout": timed_out,
        "stdout": file_record(output / "stdout.bin"),
        "stderr": file_record(output / "stderr.bin"),
        "completed": returncode == 0
        and not timed_out
        and launch_error is None
        and receipts_verified,
    }
    create_json(output / "completion.json", record)
    return record


def supervise(args):
    args.output_dir = Path(args.output_dir).resolve()
    args.checkpoint = Path(args.checkpoint).resolve(strict=True)
    args.python = Path(args.python).resolve(strict=True)
    snapshot = args.checkpoint.parent
    if (
        args.output_dir == snapshot
        or snapshot in args.output_dir.parents
        or args.output_dir in args.checkpoint.parents
    ):
        raise ValueError("Output must not be inside or contain the sealed checkpoint snapshot")
    if not args.python.is_file() or not 1 <= args.timeout_seconds <= 300:
        raise ValueError("Expected interpreter file and bounded worker timeout")
    if args.backend == "pread" and args.expected_safetensors_version != "0.8.0":
        raise ValueError("This pread diagnostic is only declared for official 0.8.0")
    source_before = file_record(__file__)
    if source_before["sha256"] != args.source_sha256:
        raise ValueError("Probe source SHA differs from declared source")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": 1,
        "contract": CONTRACT,
        "source_before": source_before,
        "controller_pid": os.getpid(),
        "backend": args.backend,
        "expected_safetensors_version": args.expected_safetensors_version,
        "phases": [],
        "completed": False,
        **CLAIMS,
    }
    create_json(
        args.output_dir / "invocation.json",
        {
            "checkpoint": str(args.checkpoint),
            "python": str(args.python),
            "source": source_before,
            "backend": args.backend,
            "timeout_seconds": args.timeout_seconds,
        },
    )
    try:
        checkpoint_before, header = verify_checkpoint(args.checkpoint)
        report.update({"checkpoint_before": checkpoint_before, "header": header})
        for phase in ("metadata", "slice"):
            if file_record(__file__) != source_before or inspect_header(args.checkpoint) != header:
                raise ValueError("Source/header drift before worker phase")
            record = run_phase(args, phase, source_before)
            report["phases"].append(record)
            if not record["completed"]:
                report["remaining_phases_skipped_after_failure"] = True
                break
        report["checkpoint_after"] = file_record(args.checkpoint)
        report["source_after"] = file_record(__file__)
        report["source_and_checkpoint_unchanged"] = (
            report["checkpoint_after"] == checkpoint_before
            and report["source_after"] == source_before
            and inspect_header(args.checkpoint) == header
        )
        report["completed"] = (
            len(report["phases"]) == 2
            and all(phase["completed"] for phase in report["phases"])
            and report["source_and_checkpoint_unchanged"]
        )
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        report["completed"] = False
    finally:
        for key, path in (("source_after", Path(__file__)), ("checkpoint_after", args.checkpoint)):
            if key not in report:
                try:
                    report[key] = file_record(path)
                except Exception as exc:
                    report[key] = None
                    report[f"{key}_error"] = type(exc).__name__
        if "source_and_checkpoint_unchanged" not in report:
            report["source_and_checkpoint_unchanged"] = (
                report["source_after"] == source_before
                and "checkpoint_before" in report
                and report["checkpoint_after"] == report["checkpoint_before"]
            )
    create_json(args.output_dir / "report.json", report)
    emit({"report": str(args.output_dir / "report.json"), "completed": report["completed"]})
    return 0 if report["completed"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--backend", choices=("default", "mmap", "pread"), default="default")
    parser.add_argument("--expected-safetensors-version", default="0.4.5")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--worker-phase", choices=("metadata", "slice"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.worker_phase:
            return worker(args)
        if args.python is None or args.output_dir is None:
            raise ValueError("Controller requires --python and create-only --output-dir")
        return supervise(args)
    except Exception as exc:
        emit({"event": "storage_probe_failed", "type": type(exc).__name__, "message": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
