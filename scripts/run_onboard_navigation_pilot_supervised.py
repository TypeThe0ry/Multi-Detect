"""Create-only external supervision of the frozen navigation pilot process.

The controller imports no torch/transformers and survives a native child crash.
Load-only initializes the existing NavigationBackend without inference/backward
or an optimizer. No retry, policy promotion, transport or flight action exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

WORKSPACE = Path(__file__).resolve().parents[1]
TRAINER = WORKSPACE / "scripts/train_onboard_vlm_navigation_pilot.py"
CONTRACT = "multidetect.onboard-navigation-pilot-supervision-v1"
LOAD_CONTRACT = "multidetect.onboard-navigation-pilot-load-only-v1"
CLAIMS = {
    "quality_claim": False,
    "candidate_promoted": False,
    "physical_performance_claimed": False,
    "hil_real_evidence": False,
    "outdoor_gnss_evidence": False,
    "final_release_allowed": False,
    "execution_authority": False,
    "transport_created": False,
    "mission_created": False,
}
COUNTERS = (
    "optimizer_steps_attempted",
    "optimizer_steps_returned",
    "optimizer_steps_synchronized",
    "optimizer_steps_completed",
)


def now():
    return datetime.now(UTC).isoformat()


def strict_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def reject(value):
        raise ValueError(f"Non-finite JSON constant {value}")

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


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError("Source binding must identify a file")
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def create_json(path, value):
    with Path(path).open("xb") as stream:
        stream.write(
            json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode("utf-8")
            + b"\n"
        )
        stream.flush()
        os.fsync(stream.fileno())


def emit(stage, **fields):
    print(json.dumps({"event": stage, **fields}, allow_nan=False), flush=True)


def resolve_fresh(path, root):
    path = Path(path).absolute()
    if path.exists() or path.is_symlink():
        raise FileExistsError("Output directories must be new; no previous attempt is overwritten")
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(root) or resolved == root:
        raise ValueError("Output must be a new directory below artifact_root")
    return resolved


def protect_sealed_directories(output, plan, root):
    """New evidence must not add files to sealed base or dataset directories."""
    protected = [Path(plan["base"]["snapshot"]).resolve(strict=True)]
    for name in ("dataset_report", "dataset_samples", "review_receipt"):
        source = Path(plan[name]["path"])
        source = (source if source.is_absolute() else root / source).resolve(strict=True)
        protected.append(source.parent)
    if any(output.is_relative_to(directory) for directory in protected):
        raise ValueError("Output cannot be inside a sealed base/dataset/review directory")


def read_plan(path, expected):
    if (
        not isinstance(expected, str)
        or len(expected) != 64
        or any(char not in "0123456789abcdef" for char in expected)
    ):
        raise ValueError("Plan SHA must be canonical lowercase SHA-256")
    path = Path(path).resolve(strict=True)
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Plan exceeds its frozen CLI size cap")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("Plan differs from the invocation SHA")
    plan = strict_json(raw)
    if not isinstance(plan, dict) or not Path(plan["artifact_root"]).is_absolute():
        raise ValueError("Plan must declare an absolute artifact_root")
    root = Path(plan["artifact_root"]).resolve(strict=True)
    if not root.is_dir() or not path.is_relative_to(root):
        raise ValueError("Plan must be inside its existing artifact_root")
    return plan, root, path


def capture_sources(plan, root, plan_path):
    """Copy exact existing plan bindings; the frozen child owns full admission."""
    expected = [
        plan["dataset_report"],
        plan["dataset_samples"],
        plan["review_receipt"],
        *plan["source_files"],
        plan["base"]["safetensors"],
        *plan["base"]["processor_files"],
    ]
    records = {}
    for bound in expected:
        if not isinstance(bound, dict) or set(bound) != {"path", "bytes", "sha256"}:
            raise ValueError("Unexpected frozen source binding")
        source = Path(bound["path"])
        source = (source if source.is_absolute() else root / source).resolve(strict=True)
        if not source.is_relative_to(root):
            raise ValueError("Frozen planned source escaped artifact_root")
        actual = file_record(source)
        if actual["bytes"] != bound["bytes"] or actual["sha256"] != bound["sha256"]:
            raise ValueError("Frozen planned source differs before/after child")
        records[str(source)] = actual
    for source in (plan_path, Path(__file__), TRAINER, Path(sys.executable)):
        actual = file_record(source)
        records[actual["path"]] = actual
    test_source = WORKSPACE / "tests/test_run_onboard_navigation_pilot_supervised.py"
    if test_source.is_file():
        actual = file_record(test_source)
        records[actual["path"]] = actual
    return [records[key] for key in sorted(records)]


def child_command(args, plan_path):
    prefix = [sys.executable, "-u", "-B", "-X", "utf8", "-X", "faulthandler"]
    parameters = [
        "--plan",
        str(plan_path),
        "--plan-sha256",
        args.plan_sha256,
        "--output-dir",
        str(args.output_dir),
    ]
    if args.mode == "train":
        return [*prefix, str(TRAINER), *parameters]
    return [*prefix, str(Path(__file__).resolve()), "--worker-load-only", *parameters]


def read_worker_report(output, mode, *, expected_plan_sha256):
    path = output / ("load-only-report.json" if mode == "load-only" else "report.json")
    result = {"exists": path.is_file(), "path": str(path), "valid": False, "counters": None}
    if not result["exists"]:
        return result
    try:
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Worker report exceeds cap")
        value = strict_json(path.read_bytes())
        if not isinstance(value, dict):
            raise ValueError("Worker report must be an object")
        result["binding"] = file_record(path)
        result["declared_native_exit_code"] = value.get("native_exit_code")
        result["claims_false"] = all(value.get(key) is False for key in CLAIMS)
        plan_field = "plan_sha256" if mode == "load-only" else "training_plan_sha256"
        result["report_plan_sha256"] = value.get(plan_field)
        expected_contract = (
            LOAD_CONTRACT
            if mode == "load-only"
            else ("multidetect.onboard-vlm-navigation-pilot-training-v1")
        )
        result["valid"] = (
            value.get("contract") == expected_contract
            and type(value.get("native_exit_code")) is int
            and value["native_exit_code"] == 0
            and result["claims_false"]
            and result["report_plan_sha256"] == expected_plan_sha256
        )
        if mode == "load-only":
            result["valid"] = result["valid"] and value.get("backend_initialized") is True
            result["valid"] = result["valid"] and all(
                value.get(field) is False
                for field in (
                    "optimizer_performed",
                    "inference_performed",
                    "forward_backward_performed",
                )
            )
        elif all(type(value.get(key)) is int and value[key] >= 0 for key in COUNTERS):
            result["counters"] = {key: value[key] for key in COUNTERS}
        else:
            result["valid"] = False
    except Exception as exc:
        result["failure_type"] = type(exc).__name__
        result["failure"] = str(exc)
    return result


def journal_counters(output, *, expected_sample_ids=None):
    """Observed fsynced records only; absence never proves zero optimizer work."""
    path = output / "training-events.jsonl"
    if not path.is_file() or path.stat().st_size == 0:
        return {"exists": path.is_file(), "observed_counters": None}
    result = {"exists": True, "observed_counters": None, "binding": file_record(path)}
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("Worker step journal exceeds bounded pilot cap")
        counts = dict.fromkeys(COUNTERS, 0)
        stages = {key.replace("optimizer_steps_", "optimizer_step_"): key for key in COUNTERS}
        ordered_stages = tuple(stages)
        expected_step, stage_index, active_sample = 1, 0, None
        seen_samples = set()
        with path.open("rb") as stream:
            for line in stream:
                row = strict_json(line)
                if not isinstance(row, dict) or row.get("stage") not in stages:
                    raise ValueError("Unknown/incomplete recorded optimizer event")
                if (
                    type(row.get("step")) is not int
                    or row["step"] != expected_step
                    or row.get("stage") != ordered_stages[stage_index]
                    or row.get("split") != "train"
                    or type(row.get("epoch")) is not int
                    or row["epoch"] != 1
                    or not isinstance(row.get("sample_id"), str)
                    or not row["sample_id"]
                ):
                    raise ValueError("Optimizer event order/step/identity differs")
                if stage_index == 0:
                    active_sample = row["sample_id"]
                    if expected_sample_ids is not None and (
                        expected_step > len(expected_sample_ids)
                        or active_sample != expected_sample_ids[expected_step - 1]
                    ):
                        raise ValueError(
                            "Optimizer journal differs from frozen shuffled sample IDs"
                        )
                    if active_sample in seen_samples:
                        raise ValueError("One-epoch optimizer journal repeats a sample")
                    seen_samples.add(active_sample)
                elif row["sample_id"] != active_sample:
                    raise ValueError("Optimizer stages name different samples")
                counts[stages[row["stage"]]] += 1
                stage_index += 1
                if stage_index == len(ordered_stages):
                    stage_index, active_sample = 0, None
                    expected_step += 1
        result["observed_counters"] = counts
    except Exception as exc:
        result["failure_type"] = type(exc).__name__
        result["failure"] = str(exc)
    return result


def supervise(args, *, popen_factory=subprocess.Popen):
    plan, root, plan_path = read_plan(args.plan, args.plan_sha256)
    output = resolve_fresh(args.output_dir, root)
    supervisor = resolve_fresh(args.supervisor_dir, root)
    protect_sealed_directories(output, plan, root)
    protect_sealed_directories(supervisor, plan, root)
    if output.is_relative_to(supervisor) or supervisor.is_relative_to(output):
        raise ValueError("Supervisor and worker directories must not overlap")
    supervisor.mkdir(parents=True, exist_ok=False)
    completion = {
        "schema_version": 1,
        "contract": CONTRACT,
        **CLAIMS,
        "mode": args.mode,
        "started_at": now(),
        "ended_at": None,
        "child_pid": None,
        "child_returncode_raw": None,
        "child_returncode_uint32": None,
        "child_returncode_signed32": None,
        "child_returncode_hex32": None,
        "supervisor_exit_code": 1,
        "source_bindings_unchanged": False,
        "child_completion_verified": False,
        "automatic_retry_performed": False,
        "timeout_used": False,
        "external_processes_stopped": False,
        "worker_report": None,
    }
    try:
        before = capture_sources(plan, root, plan_path)
        completion["sources_before"] = before
        child_args = SimpleNamespace(
            mode=args.mode, output_dir=output, plan_sha256=args.plan_sha256
        )
        command = child_command(child_args, plan_path)
        completion["argv"] = command
        completion["cwd"] = str(WORKSPACE)
        create_json(supervisor / "preflight.json", completion)
        with (
            (supervisor / "stdout.bin").open("xb", buffering=0) as stdout,
            (supervisor / "stderr.bin").open("xb", buffering=0) as stderr,
        ):
            child = popen_factory(
                command,
                shell=False,
                cwd=str(WORKSPACE),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
            )
            completion["child_pid"] = child.pid
            # Once launched, observe the exact child to terminal even if an
            # auxiliary progress write fails; never abandon a live GPU process.
            try:
                create_json(supervisor / "child-started.json", completion)
                emit("navigation_pilot_child_started", mode=args.mode, pid=child.pid)
            except Exception as exc:
                completion["progress_recording_failure"] = type(exc).__name__
            finally:
                returned = child.wait()
            completion["child_returncode_raw"] = returned
            code = returned & 0xFFFFFFFF
            completion["child_returncode_uint32"] = code
            completion["child_returncode_signed32"] = code if code < 0x80000000 else code - 2**32
            completion["child_returncode_hex32"] = f"0x{code:08x}"
        completion["stdout"] = file_record(supervisor / "stdout.bin")
        completion["stderr"] = file_record(supervisor / "stderr.bin")
        completion["worker_report"] = read_worker_report(
            output, args.mode, expected_plan_sha256=args.plan_sha256
        )
        expected_ids = None
        if args.mode == "train":
            expected_ids = list(plan["train_sample_ids"])
            random.Random(plan["optimization"]["seed"]).shuffle(expected_ids)
        completion["optimizer_journal"] = journal_counters(output, expected_sample_ids=expected_ids)
        after = capture_sources(plan, root, plan_path)
        completion["sources_after"] = after
        completion["source_bindings_unchanged"] = before == after
        report = completion["worker_report"]
        valid = returned == 0 and report["valid"] and before == after
        if args.mode == "train" and report["counters"] is not None:
            valid = valid and all(
                count == plan["expected_samples"] for count in report["counters"].values()
            )
            valid = valid and (
                completion["optimizer_journal"]["observed_counters"] == report["counters"]
            )
        completion["child_completion_verified"] = valid
        completion["supervisor_exit_code"] = 0 if valid else 1
    except Exception as exc:
        completion["failure_type"] = type(exc).__name__
        completion["failure"] = str(exc)
    completion["ended_at"] = now()
    create_json(supervisor / "completion.json", completion)
    emit(
        "navigation_pilot_supervisor_finished",
        pid=completion["child_pid"],
        child_returncode=completion["child_returncode_raw"],
        supervisor_exit_code=completion["supervisor_exit_code"],
        child_completion_verified=completion["child_completion_verified"],
    )
    return completion["supervisor_exit_code"]


def worker_load_only(args):
    """Child-only entry; an access violation is preserved by the outside supervisor."""
    sys.path.insert(0, str(WORKSPACE))
    sys.path.insert(0, str(WORKSPACE / "src"))
    emit("load_only_plan_path_verification_started", pid=os.getpid())
    original_plan, root, _ = read_plan(args.plan, args.plan_sha256)
    output = resolve_fresh(args.output_dir, root)
    protect_sealed_directories(output, original_plan, root)
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": 1,
        "contract": LOAD_CONTRACT,
        **CLAIMS,
        "native_exit_code": 1,
        "backend_initialized": False,
        "optimizer_performed": False,
        "inference_performed": False,
        "forward_backward_performed": False,
        "plan_sha256": args.plan_sha256,
        "started_at": now(),
        "ended_at": None,
    }
    try:
        emit("load_only_import_trainer_started", pid=os.getpid())
        from scripts import train_onboard_vlm_navigation_pilot as trainer

        emit("load_only_plan_verification_started")
        plan = trainer.load_plan(args.plan, args.plan_sha256)
        budget = trainer.validate_plan_shape(plan)
        trainer.verify_plan_sources(plan)
        emit("load_only_plan_and_sources_verified")
        samples, _ = trainer.admit_dataset(plan)
        emit("load_only_dataset_admitted", samples=len(samples))
        emit("load_only_backend_initialization_started")
        backend = trainer.NavigationBackend(Path(plan["base"]["snapshot"]), budget)
        report["backend_initialized"] = True
        report["pre_load_cuda_memory"] = backend.pre_load_cuda_memory
        report["base_parameter_sha256"] = backend.base_digest_initial
        report["adapter_parameter_sha256"] = backend.adapter_digest_initial
        report["trainable_audit"] = backend.trainable_audit
        emit("load_only_backend_initialized")
        trainer.verify_plan_sources(plan)
        if sha256_file(args.plan) != args.plan_sha256:
            raise ValueError("Plan changed during load-only")
        emit("load_only_sources_reverified")
        report["native_exit_code"] = 0
    except Exception as exc:
        report["failure_type"] = type(exc).__name__
        report["failure"] = str(exc)
        if hasattr(exc, "pre_load_cuda_memory"):
            report["pre_load_cuda_memory"] = exc.pre_load_cuda_memory
    report["ended_at"] = now()
    create_json(output / "load-only-report.json", report)
    emit("load_only_finished", native_exit_code=report["native_exit_code"])
    return report["native_exit_code"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--supervisor-dir", type=Path)
    parser.add_argument("--mode", choices=("load-only", "train"), default="load-only")
    parser.add_argument("--worker-load-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.worker_load_only:
            return worker_load_only(args)
        if args.supervisor_dir is None:
            raise ValueError("External supervision requires --supervisor-dir")
        return supervise(args)
    except Exception as exc:
        emit("navigation_pilot_supervisor_invocation_failed", failure_type=type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
