"""Additive child-only allocator performance workaround for the sealed pilot.

The frozen streaming module's __file__ is scoped to this dispatch entrypoint,
not edited on disk. Its actual implementation is separately hash-bound. Only
optional caching-allocator preallocation is bypassed; loading/training is not.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import importlib
import inspect
import os
import sys
import traceback
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))
from scripts import run_onboard_navigation_pilot_streaming as streaming  # noqa: E402

sup = streaming.sup
BODY_SOURCE = Path(streaming.__file__).resolve(strict=True)
BODY_SHA256 = "5289c8fa44c342c1b689c702873bdbb7a2924932d51b8486b0ef15bae33a4c66"
TRAINER_SOURCE = sup.TRAINER.resolve(strict=True)
CONTRACT = "multidetect.onboard-navigation-streaming-no-warmup-runtime-v1"
PARAMETERS = ["model", "expanded_device_map", "hf_quantizer"]
POLICY = "skip_optional_caching_allocator_warmup"
SCOPE = "this_child_only_performance_preallocation"
DECLARATIONS = {
    "warmup_policy": POLICY,
    "warmup_scope": SCOPE,
    "entrypoint_dispatch_only": True,
    "model_weights_loading_skipped": False,
    "quantization_changed": False,
    "device_map_changed": False,
    "training_budget_changed": False,
    "constructor_observability_scope": "this_child_only_original_init_passthrough_traceback",
    "backend_initialization_changed": False,
}


def canonical_function_source(text):
    return text.replace("\r\n", "\n").rstrip("\n") + "\n"


def function_binding(path):
    """Obtain the exact optional function identity without importing Torch/HF."""
    path = Path(path).resolve(strict=True)
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Allocator module source exceeds its bound")
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "caching_allocator_warmup"
    ]
    if len(nodes) != 1:
        raise ValueError("Expected exactly one allocator warmup function")
    node = nodes[0]
    args = node.args
    if (
        node.decorator_list
        or args.posonlyargs
        or args.kwonlyargs
        or args.defaults
        or args.vararg
        or args.kwarg
        or [arg.arg for arg in args.args] != PARAMETERS
    ):
        raise ValueError("Allocator warmup source signature differs")
    source = canonical_function_source(
        "".join(text.splitlines(keepends=True)[node.lineno - 1 : node.end_lineno])
    )
    return {
        "source": sup.file_record(path),
        "function_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "parameter_names": PARAMETERS.copy(),
        "first_lineno": node.lineno,
    }


def validate_manifest(manifest, records):
    by_path = {record["path"]: record for record in records}
    if (
        by_path.get(str(BODY_SOURCE)) != sup.file_record(BODY_SOURCE)
        or sup.sha256_file(BODY_SOURCE) != BODY_SHA256
    ):
        raise ValueError("Frozen streaming implementation must be explicitly bound")
    if by_path.get(str(TRAINER_SOURCE)) != sup.file_record(TRAINER_SOURCE):
        raise ValueError("Original trainer must be bound for constructor observability")
    if manifest.get("warmup_policy") != POLICY or manifest.get("warmup_scope") != SCOPE:
        raise ValueError("Warmup policy/scope differs")
    bound = manifest.get("allocator_warmup_source")
    if not isinstance(bound, dict) or set(bound) != {"path", "bytes", "sha256"}:
        raise ValueError("Warmup module source binding is required")
    actual = function_binding(bound["path"])
    if (
        by_path.get(bound["path"]) != bound
        or actual["source"] != bound
        or manifest.get("allocator_warmup_function_sha256") != actual["function_sha256"]
        or manifest.get("allocator_warmup_parameter_names") != PARAMETERS
    ):
        raise ValueError("Warmup module/function/signature identity differs")
    return actual


@contextlib.contextmanager
def dispatch_scope(args, *, installer=None):
    old = (
        streaming.CONTRACT,
        streaming.__file__,
        streaming.read_runtime,
        streaming.install_streaming_loader,
    )

    def checked(arguments):
        manifest, plan, root, records = old[2](arguments)
        validate_manifest(manifest, records)
        return manifest, plan, root, records

    try:
        streaming.CONTRACT, streaming.__file__, streaming.read_runtime = CONTRACT, __file__, checked
        if installer is not None:
            streaming.install_streaming_loader = installer
        yield
    finally:
        (
            streaming.CONTRACT,
            streaming.__file__,
            streaming.read_runtime,
            streaming.install_streaming_loader,
        ) = old


def install_warmup_skip(manifest):
    """Called only after the streaming aliases are installed in this child."""
    mu = importlib.import_module("transformers.modeling_utils")
    torch = importlib.import_module("torch")
    binding = function_binding(manifest["allocator_warmup_source"]["path"])
    original = mu.caching_allocator_warmup
    signature = inspect.signature(original)
    source_sha = hashlib.sha256(
        canonical_function_source(inspect.getsource(original)).encode()
    ).hexdigest()
    if (
        Path(mu.__file__).resolve(strict=True) != Path(binding["source"]["path"])
        or Path(original.__code__.co_filename).resolve(strict=True)
        != Path(binding["source"]["path"])
        or original.__globals__ is not mu.__dict__
        or original.__code__.co_firstlineno != binding["first_lineno"]
        or source_sha != binding["function_sha256"]
        or source_sha != manifest["allocator_warmup_function_sha256"]
        or list(signature.parameters) != PARAMETERS
        or any(
            parameter.kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD
            or parameter.default is not inspect.Parameter.empty
            for parameter in signature.parameters.values()
        )
    ):
        raise ValueError("Imported allocator warmup function identity differs")
    state = {
        "pid": os.getpid(),
        "allocator_warmup_source": binding["source"],
        "allocator_warmup_function_sha256": source_sha,
        "allocator_warmup_parameter_names": PARAMETERS.copy(),
        "warmup_calls_attempted": 0,
        "warmup_calls_skipped": 0,
        "warmup_override_installed": True,
        "warmup_override_restored": False,
        **DECLARATIONS,
    }

    def skip(model, expanded_device_map, hf_quantizer):
        state["warmup_calls_attempted"] += 1
        try:
            if (
                hf_quantizer is not None
                or not isinstance(expanded_device_map, dict)
                or not expanded_device_map
            ):
                raise ValueError("Warmup bypass only admits unquantized CUDA:0 model loading")
            for device in expanded_device_map.values():
                valid = (
                    device == "cuda:0"
                    if isinstance(device, str)
                    else (
                        type(device) is int
                        and device == 0
                        or isinstance(device, torch.device)
                        and device.type == "cuda"
                        and device.index in (None, 0)
                    )
                )
                if not valid:
                    raise ValueError("Warmup bypass cannot change CPU/other-device paths")
            if model.__class__.__name__ != "Qwen3VLForConditionalGeneration":
                raise ValueError("Warmup bypass only admits the original Qwen3VL model class")
            if torch.distributed.is_available() and torch.distributed.is_initialized():
                raise ValueError("Warmup bypass does not admit distributed loading")
            state["warmup_calls_skipped"] += 1
            sup.emit("allocator_warmup_skipped", **state)
            return None
        except Exception:
            traceback.print_exc()
            sup.emit("allocator_warmup_rejected", **state)
            raise

    mu.caching_allocator_warmup = skip
    sup.emit("allocator_warmup_override_installed", **state)

    def restore():
        mu.caching_allocator_warmup = original
        state["warmup_override_restored"] = mu.caching_allocator_warmup is original
        sup.emit("allocator_warmup_override_restored", **state)

    return state, restore


def install_constructor_traceback(manifest):
    """Preserve the original initializer and its exception; only print its stack."""
    trainer = importlib.import_module("scripts.train_onboard_vlm_navigation_pilot")
    bound = sup.file_record(TRAINER_SOURCE)
    if bound not in manifest["source_files"]:
        raise ValueError("Constructor observability source is not runtime-bound")
    original = trainer.NavigationBackend.__init__
    if (
        Path(trainer.__file__).resolve(strict=True) != TRAINER_SOURCE
        or Path(original.__code__.co_filename).resolve(strict=True) != TRAINER_SOURCE
        or original.__globals__ is not trainer.__dict__
        or list(inspect.signature(original).parameters) != ["self", "snapshot", "budget"]
    ):
        raise ValueError("Original NavigationBackend constructor identity differs")
    state = {
        "source": bound,
        "constructor_source_sha256": hashlib.sha256(
            canonical_function_source(inspect.getsource(original)).encode()
        ).hexdigest(),
        "calls_attempted": 0,
        "exceptions_observed": 0,
        "installed": True,
        "restored": False,
        "scope": DECLARATIONS["constructor_observability_scope"],
    }

    def traced(self, *args, **kwargs):
        state["calls_attempted"] += 1
        try:
            return original(self, *args, **kwargs)
        except Exception:
            state["exceptions_observed"] += 1
            traceback.print_exc()
            raise

    trainer.NavigationBackend.__init__ = traced

    def restore():
        trainer.NavigationBackend.__init__ = original
        state["restored"] = trainer.NavigationBackend.__init__ is original

    return state, restore


def worker(args):
    # Admission must finish before entering any finally that can create evidence.
    # A rejected invocation must never add files to an existing user directory.
    with dispatch_scope(args):
        manifest, plan, root, _ = streaming.read_runtime(args)
        output = sup.resolve_fresh(args.output_dir, root)
        sup.protect_sealed_directories(output, plan, root)
        streaming.protect_runtime_output(output, args, manifest)
    state, restore, constructor_state, constructor_restore = None, None, None, None
    original_installer = streaming.install_streaming_loader
    returned = 1

    def installer(allowed, openings):
        nonlocal state, restore, constructor_state, constructor_restore
        counts = original_installer(allowed, openings)
        manifest, _, _, _ = streaming.read_runtime(args)
        state, restore = install_warmup_skip(manifest)
        constructor_state, constructor_restore = install_constructor_traceback(manifest)
        return counts

    try:
        with dispatch_scope(args, installer=installer):
            returned = streaming.worker(args)
    except Exception:
        traceback.print_exc()
        returned = 1
    finally:
        if constructor_restore is not None:
            try:
                constructor_restore()
            except Exception:
                traceback.print_exc()
                returned = 1
        if restore is not None:
            try:
                restore()
            except Exception:
                traceback.print_exc()
                returned = 1
        if (
            state is None
            or state["warmup_calls_attempted"] != state["warmup_calls_skipped"]
            or not state["warmup_calls_skipped"]
        ):
            returned = 1
        report = {
            "contract": CONTRACT,
            **sup.CLAIMS,
            **DECLARATIONS,
            "mode": args.mode,
            "plan_sha256": args.plan_sha256,
            "runtime_manifest_sha256": args.runtime_manifest_sha256,
            "worker_returncode": returned,
            "warmup_observation": state,
            "constructor_observation": constructor_state,
        }
        sup.emit("no_warmup_worker_finished", **report)
        if output.is_dir():
            sup.create_json(output / "allocator-warmup-report.json", report)
    return returned


def verify_warmup(path, log, args, manifest):
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Actual allocator warmup report is missing/oversized")
    report = sup.strict_json(path.read_bytes())
    state = report.get("warmup_observation") if isinstance(report, dict) else None
    constructor = report.get("constructor_observation") if isinstance(report, dict) else None
    if (
        not isinstance(constructor, dict)
        or constructor.get("source") != sup.file_record(TRAINER_SOURCE)
        or constructor.get("scope") != DECLARATIONS["constructor_observability_scope"]
        or constructor.get("installed") is not True
        or constructor.get("restored") is not True
        or type(constructor.get("calls_attempted")) is not int
        or constructor["calls_attempted"] <= 0
        or type(constructor.get("exceptions_observed")) is not int
        or constructor["exceptions_observed"] != 0
    ):
        raise ValueError("Original constructor pass-through observation differs")
    if (
        not isinstance(state, dict)
        or report.get("contract") != CONTRACT
        or report.get("mode") != args.mode
        or report.get("plan_sha256") != args.plan_sha256
        or report.get("runtime_manifest_sha256") != args.runtime_manifest_sha256
        or type(report.get("worker_returncode")) is not int
        or report["worker_returncode"] != 0
        or any(report.get(key) is not False for key in sup.CLAIMS)
        or any(
            type(report.get(key)) is not type(value) or report[key] != value
            for key, value in DECLARATIONS.items()
        )
        or type(state.get("pid")) is not int
        or state["pid"] <= 0
        or state.get("allocator_warmup_source") != manifest["allocator_warmup_source"]
        or state.get("allocator_warmup_function_sha256")
        != manifest["allocator_warmup_function_sha256"]
        or state.get("allocator_warmup_parameter_names") != PARAMETERS
        or state.get("warmup_override_installed") is not True
        or state.get("warmup_override_restored") is not True
        or any(
            type(state.get(key)) is not type(value) or state[key] != value
            for key, value in DECLARATIONS.items()
        )
        or any(
            type(state.get(key)) is not int or state[key] <= 0
            for key in ("warmup_calls_attempted", "warmup_calls_skipped")
        )
        or state["warmup_calls_attempted"] != state["warmup_calls_skipped"]
    ):
        raise ValueError("Warmup report identity/counters/scope differs")
    if not log.is_file() or log.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Actual allocator warmup event log missing/oversized")
    installed, restored, finished, calls, runtime_pid = False, False, False, 0, None
    for line in log.read_bytes().splitlines():
        if not line.lstrip().startswith(b"{"):
            continue
        row = sup.strict_json(line)
        event = row.get("event") if isinstance(row, dict) else None
        if event == "streaming_runtime_verified":
            if runtime_pid is not None or installed or finished or row.get("pid") != state["pid"]:
                raise ValueError("Warmup runtime PID event must be unique and precede installation")
            runtime_pid = row.get("pid")
        elif event == "no_warmup_worker_finished":
            if finished or not restored or {k: v for k, v in row.items() if k != "event"} != report:
                raise ValueError("Warmup finished event differs")
            finished = True
        elif event in (
            "allocator_warmup_override_installed",
            "allocator_warmup_skipped",
            "allocator_warmup_override_restored",
        ):
            if (
                row.get("pid") != state["pid"]
                or runtime_pid != state["pid"]
                or any(
                    row.get(key) != state[key]
                    for key in (
                        "allocator_warmup_source",
                        "allocator_warmup_function_sha256",
                        "allocator_warmup_parameter_names",
                    )
                )
                or any(
                    type(row.get(key)) is not type(value) or row[key] != value
                    for key, value in DECLARATIONS.items()
                )
            ):
                raise ValueError("Warmup event source/pid/scope differs")
            if event.endswith("installed") and not installed and not restored:
                installed = True
            elif event == "allocator_warmup_skipped" and installed and not restored:
                calls += 1
            elif event.endswith("restored") and installed and not restored:
                restored = True
            else:
                raise ValueError("Warmup event order differs")
            if (
                row.get("warmup_override_installed") is not True
                or row.get("warmup_override_restored") is not restored
            ):
                raise ValueError("Warmup event override/restoration flags differ")
            if any(
                type(row.get(key)) is not int or row[key] != calls
                for key in ("warmup_calls_attempted", "warmup_calls_skipped")
            ):
                raise ValueError("Warmup event counters differ")
    if not finished or not calls or calls != state["warmup_calls_skipped"]:
        raise ValueError("Warmup bypass has no corresponding actual call evidence")
    return report


def supervise(args, *, popen_factory=sup.subprocess.Popen):
    with dispatch_scope(args):
        manifest, _, _, _ = streaming.read_runtime(args)
        returned = streaming.supervise(args, popen_factory=popen_factory)
    valid, failure = False, None
    try:
        report = verify_warmup(
            Path(args.output_dir) / "allocator-warmup-report.json",
            Path(args.supervisor_dir) / "stdout.bin",
            args,
            manifest,
        )
        valid = True
    except Exception as exc:
        report = None
        failure = {"type": type(exc).__name__, "message": str(exc)}
    result = {
        "contract": CONTRACT,
        **sup.CLAIMS,
        **DECLARATIONS,
        "mode": args.mode,
        "runtime_manifest_sha256": args.runtime_manifest_sha256,
        "streaming_supervisor_exit_code": returned,
        "warmup_call_evidence_valid": valid,
        "warmup_report": report,
        "warmup_failure": failure,
        "supervisor_exit_code": 0 if returned == 0 and valid else 1,
    }
    sup.create_json(Path(args.supervisor_dir) / "allocator-warmup-completion.json", result)
    return result["supervisor_exit_code"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--supervisor-dir", type=Path)
    parser.add_argument("--mode", choices=("load-only", "train"), default="load-only")
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--runtime-manifest-sha256", required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.worker:
            return worker(args)
        if args.supervisor_dir is None:
            raise ValueError("Parent requires a fresh supervisor directory")
        return supervise(args)
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
