"""Opt-in exact-load-only diagnostics; import and plan emission are stdlib-only.

This wraps the frozen real load_preloaded context, not its test-only operations.
Only external boundaries are visible. In particular the prior warmup restoration
event precedes reader cleanup, head loading and identity checks; it does not
prove that from_pretrained itself crashed. No camera, inference or flight occurs.
The caller owns an external process watchdog. Native failure may leave only the
durable phase prefix and faulthandler log, never a synthetic success report.
"""

from __future__ import annotations

import argparse
import faulthandler
import hashlib
import importlib
import json
import math
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(1, str(ROOT / "src"))

CONTRACT = "multidetect.onboard-navigation-exact-load-diagnostic-v1"
R3_PLAN = "configs/training/onboard-navigation-live-shadow-20261008-r3.json"
PINS = {
    R3_PLAN: "75951245c72e3189d545dc7cef866e98bd7bd7901c30273e9dec05fc5f6b9d5f",
    "scripts/onboard_navigation_live_loader.py": (
        "5024308f500596a060186874804c9c0179c778c61b3f6eefe6b3cc73b29b56fe"
    ),
    "scripts/onboard_navigation_host_memory.py": (
        "9298e17ef626ceea4b1e2f2abec6304a8846e537e29d5555db4fc02a4954690a"
    ),
    "docs/evidence/onboard-navigation-live-feature-attempt-20261008-r3.json": (
        "ff0ac264c1441001f1cba15070a20ddb6ea98b4e33c7cf4c7bcee90164aa59be"
    ),
}
NEW_FILES = (
    "scripts/diagnose_onboard_navigation_exact_load.py",
    "tests/test_diagnose_onboard_navigation_exact_load.py",
)
MAX_SMALL = 8 * 1024 * 1024
MAX_PLAN = 1024 * 1024
MAX_REPORT = 2 * 1024 * 1024
LIMITS = {"max_events": 32, "max_event_bytes": 16384, "max_log_bytes": 262144}
FORBIDDEN_PRELOAD = (
    "torch",
    "transformers",
    "peft",
    "accelerate",
    "safetensors",
    "numpy",
    "PIL",
    "pyarrow",
    "datasets",
)
CLAIMS = {
    "quality_claim": False,
    "navigation_accepted": False,
    "training_ready": False,
    "candidate_promoted": False,
    "flight_authority": False,
    "physical_performance_claimed": False,
    "final_release_allowed": False,
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _sha(value):
    if type(value) is not str or not re.fullmatch("[0-9a-f]{64}", value):
        raise ValueError("Explicit lowercase SHA256 required")
    return value


def _positive(value, name):
    if type(value) is not int or value < 1 or value > 2**63 - 1:
        raise ValueError("Positive integer byte threshold required: " + name)
    return value


def _json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    return json.loads(
        data,
        object_pairs_hook=pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )


def _path(value, *, external=False):
    path = Path(value)
    path = (path if path.is_absolute() else ROOT / path).resolve(strict=True)
    if not external and not path.is_relative_to(ROOT):
        raise ValueError("Repository path escape")
    if not path.is_file():
        raise ValueError("Bound input is not a regular file")
    return path


def small_record(path, *, external=False):
    path = _path(path, external=external)
    if path.stat().st_size > MAX_SMALL:
        raise ValueError("Small-source cap exceeded; no large payload read")
    before = path.stat()
    data = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    ) or len(data) != after.st_size:
        raise ValueError("Source changed while reading")
    return {"path": path.as_posix(), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _read_bound(row):
    current = small_record(row["path"])
    if current != row:
        raise ValueError("Bound small source drift")
    data = _path(row["path"]).read_bytes()
    if len(data) != row["bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise ValueError("Bound bytes changed before decode")
    return data


def _pins():
    rows = []
    for relative, digest in PINS.items():
        row = small_record(relative)
        if row["sha256"] != digest:
            raise ValueError("Frozen diagnostic dependency drift: " + relative)
        rows.append(row)
    return rows


def _bootstrap():
    _pins()
    modules = []
    for name, relative in (
        ("scripts.onboard_navigation_live_loader", "scripts/onboard_navigation_live_loader.py"),
        ("scripts.onboard_navigation_host_memory", "scripts/onboard_navigation_host_memory.py"),
    ):
        module = importlib.import_module(name)
        if Path(module.__file__).resolve() != _path(relative):
            raise ValueError("Actual stdlib helper origin differs")
        modules.append(module)
    return tuple(modules)


def inventory(rows):
    """Only explicitly declared runtime inputs may be outside ROOT.

    Small current bytes are independently hashed. Large current identities are
    stat observations, never a claim of independent full-weight SHA validation.
    The unchanged frozen loader performs its own full identity verification.
    """
    result, seen = [], {}
    for row in rows:
        if type(row) is not dict or set(row) != {"path", "bytes", "sha256"}:
            raise ValueError("Exact source declaration required")
        if type(row["bytes"]) is not int or row["bytes"] < 1:
            raise ValueError("Invalid declared source size")
        _sha(row["sha256"])
        path = _path(row["path"], external=True)
        key = path.as_posix()
        normalized = dict(row, path=key)
        if key in seen:
            if seen[key] != normalized:
                raise ValueError("Conflicting canonical source declaration")
            continue
        seen[key] = normalized
        stat = path.stat()
        if stat.st_size != row["bytes"]:
            raise ValueError("Declared source size changed")
        if row["bytes"] <= MAX_SMALL:
            if small_record(path, external=True) != normalized:
                raise ValueError("Current source SHA changed")
            result.append({"binding": normalized, "verification": "current_byte_sha256"})
        else:
            result.append(
                {
                    "binding": normalized,
                    "verification": "current_stat_only_not_full_sha",
                    "stat": {
                        "bytes": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                        "device": stat.st_dev,
                        "inode": stat.st_ino,
                    },
                }
            )
    return sorted(result, key=lambda item: item["binding"]["path"])


def output_path(value):
    path = Path(value)
    path = (path if path.is_absolute() else ROOT / path).resolve(strict=False)
    parent = (ROOT / "artifacts/diagnostics").resolve(strict=True)
    if path.parent != parent or not re.fullmatch(
        "onboard-navigation-exact-load-[A-Za-z0-9_-]+", path.name
    ):
        raise ValueError("Fresh diagnostics direct-child output required")
    if path.exists():
        raise ValueError("Create-only output already exists")
    return path


def build_plan(
    output_directory, *, min_physical_available_bytes, min_process_available_commit_bytes
):
    loader, _ = _bootstrap()
    r3 = _json(_read_bound(_pins()[0]))
    config = r3["load_config"]
    loader.validate_load_config(config)
    source_rows = [*_pins(), *(small_record(relative) for relative in NEW_FILES)]
    return {
        "contract": CONTRACT,
        "output_directory": output_path(output_directory).as_posix(),
        "inherited_r3_plan": _pins()[0],
        "load_config": config,
        "load_config_sha256": hashlib.sha256(canonical(config)).hexdigest(),
        "source_files": source_rows,
        "input_inventory": inventory(loader._all_files(config)),
        "memory_preflight": {
            "min_physical_available_bytes": _positive(
                min_physical_available_bytes, "min_physical_available_bytes"
            ),
            "min_process_available_commit_bytes": _positive(
                min_process_available_commit_bytes, "min_process_available_commit_bytes"
            ),
            "units": "bytes",
            "thresholds_are_operator_policy_not_proven_model_capacity": True,
        },
        "phase_limits": dict(LIMITS),
        "execution_policy": {
            "requires_explicit_execute": True,
            "standalone_process": True,
            "external_watchdog_required_s": 600,
            "watchdog_enforced_by_this_cli": False,
            "camera_calls": 0,
            "fused_forward_calls": 0,
            "head_forward_calls": 0,
            "generation_calls": 0,
            "optimizer_attempts": 0,
            "flight_commands": 0,
        },
        "claims": dict(CLAIMS),
    }


def validate_plan(plan):
    expected = {
        "contract",
        "output_directory",
        "inherited_r3_plan",
        "load_config",
        "load_config_sha256",
        "source_files",
        "input_inventory",
        "memory_preflight",
        "phase_limits",
        "execution_policy",
        "claims",
    }
    if type(plan) is not dict or set(plan) != expected or plan["contract"] != CONTRACT:
        raise ValueError("Exact diagnostic plan schema required")
    if canonical(plan["claims"]) != canonical(CLAIMS) or plan["phase_limits"] != LIMITS:
        raise ValueError("Diagnostic claims or limits changed")
    if canonical(plan["execution_policy"]) != canonical(
        {
            "requires_explicit_execute": True,
            "standalone_process": True,
            "external_watchdog_required_s": 600,
            "watchdog_enforced_by_this_cli": False,
            "camera_calls": 0,
            "fused_forward_calls": 0,
            "head_forward_calls": 0,
            "generation_calls": 0,
            "optimizer_attempts": 0,
            "flight_commands": 0,
        }
    ):
        raise ValueError("Only exact-load diagnostic execution permitted")
    output_path(plan["output_directory"])
    pins = _pins()
    if plan["inherited_r3_plan"] != pins[0]:
        raise ValueError("Historical R3 declaration differs")
    if plan["source_files"] != [*pins, *(small_record(relative) for relative in NEW_FILES)]:
        raise ValueError("New/frozen source inventory changed")
    original = _json(_read_bound(pins[0]))["load_config"]
    if canonical(plan["load_config"]) != canonical(original) or (
        hashlib.sha256(canonical(original)).hexdigest() != _sha(plan["load_config_sha256"])
    ):
        raise ValueError("Original exact load configuration changed")
    loader, _ = _bootstrap()
    loader.validate_load_config(original)
    if inventory(loader._all_files(original)) != plan["input_inventory"]:
        raise ValueError("Current admitted input inventory changed")
    thresholds = plan["memory_preflight"]
    if (
        type(thresholds) is not dict
        or set(thresholds)
        != {
            "min_physical_available_bytes",
            "min_process_available_commit_bytes",
            "units",
            "thresholds_are_operator_policy_not_proven_model_capacity",
        }
        or thresholds["units"] != "bytes"
        or (thresholds["thresholds_are_operator_policy_not_proven_model_capacity"] is not True)
    ):
        raise ValueError("Explicit byte preflight policy required")
    for key in ("min_physical_available_bytes", "min_process_available_commit_bytes"):
        _positive(thresholds[key], key)
    return plan


def _write_new(path, data, cap):
    if len(data) > cap:
        raise ValueError("Bounded output cap exceeded")
    with Path(path).open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    actual = small_record(path)
    if actual["bytes"] != len(data) or actual["sha256"] != hashlib.sha256(data).hexdigest():
        raise ValueError("New output readback changed")
    return actual


def emit_plan(path, plan):
    validate_plan(plan)
    path = Path(path)
    path = (path if path.is_absolute() else ROOT / path).resolve(strict=False)
    if path.parent != (ROOT / "configs/training").resolve(strict=True) or not re.fullmatch(
        "onboard-navigation-exact-load-[A-Za-z0-9_-]+\\.json", path.name
    ):
        raise ValueError("Create-only plan path required")
    return _write_new(path, canonical(plan) + b"\n", MAX_PLAN)


def admit_plan(path, digest):
    row = small_record(path)
    if row["sha256"] != _sha(digest) or row["bytes"] > MAX_PLAN:
        raise ValueError("Explicit plan SHA or byte limit differs")
    return validate_plan(_json(_read_bound(row))), row


def validate_snapshot(value):
    if type(value) is not dict or value.get("pid") != os.getpid():
        raise ValueError("Real current-process memory snapshot required")
    for key in (
        "process_working_set_bytes",
        "process_peak_working_set_bytes",
        "process_private_commit_bytes",
        "process_peak_commit_bytes",
        "host_physical_total_bytes",
        "host_physical_available_bytes",
        "process_limited_commit_limit_bytes",
        "process_available_commit_bytes",
        "process_virtual_available_bytes",
    ):
        if type(value.get(key)) is not int or value[key] < 0:
            raise ValueError("Missing/invalid Windows byte counter: " + key)
    at = value.get("observed_at_unix_s")
    if type(at) not in (int, float) or not math.isfinite(at) or at <= 0:
        raise ValueError("Invalid observation time")
    if (
        value.get("continuous_sampling") is not False
        or (value.get("os_peak_scope") != "process_lifetime")
        or value.get("GPU_memory_measured") is not False
    ):
        raise ValueError("Boundary/process-lifetime memory scope required")
    if (
        value["host_physical_available_bytes"] > value["host_physical_total_bytes"]
        or (value["process_available_commit_bytes"] > value["process_limited_commit_limit_bytes"])
        or value["process_working_set_bytes"] > value["process_peak_working_set_bytes"]
    ):
        raise ValueError("Inconsistent Windows memory counters")
    return value


class PhaseLog:
    """Held FD, per-event fsync; boundary samples, not continuous telemetry."""

    def __init__(self, stream, plan_sha256, snapshot, *, clock=time.perf_counter):
        self.stream, self.plan_sha256 = stream, _sha(plan_sha256)
        self.snapshot, self.clock = snapshot, clock
        self.count = self.bytes = 0
        self.last_at = None

    def event(self, phase, status, *, error=None, sample=True):
        if phase not in {
            "memory_preflight",
            "source_before",
            "load_context_enter",
            "loaded_context_body",
            "load_context_exit",
            "source_after",
        } or status not in {"started", "returned", "failed"}:
            raise ValueError("Only externally visible diagnostic phases allowed")
        at = self.clock()
        if (
            type(at) not in (int, float)
            or not math.isfinite(at)
            or at < 0
            or (self.last_at is not None and at < self.last_at)
        ):
            raise ValueError("Monotonic phase clock regressed")
        row = {
            "sequence": self.count + 1,
            "pid": os.getpid(),
            "plan_sha256": self.plan_sha256,
            "phase": phase,
            "status": status,
            "perf_counter_s": at,
            "clock_scope": "this_process_perf_counter_only_not_Gazebo_or_boot_time",
            "memory": validate_snapshot(self.snapshot()) if sample else None,
            "memory_not_collected_on_failure": not sample,
            "error": None
            if error is None
            else {
                "type": type(error).__name__,
                "message": str(error)[:2048],
            },
        }
        data = canonical(row) + b"\n"
        if (
            self.count >= LIMITS["max_events"]
            or len(data) > LIMITS["max_event_bytes"]
            or (self.bytes + len(data) > LIMITS["max_log_bytes"])
        ):
            raise ValueError("Durable phase log budget exhausted")
        self.stream.write(data)
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.count += 1
        self.bytes += len(data)
        self.last_at = at
        return row

    @contextmanager
    def phase(self, name):
        self.event(name, "started")
        try:
            yield
        except BaseException as error:
            try:
                self.event(name, "failed", error=error, sample=False)
            except BaseException as log_error:
                error.add_note("Diagnostic failed-event persistence also failed: " + str(log_error))
            raise
        else:
            self.event(name, "returned")


def memory_preflight(snapshot, thresholds):
    snapshot = validate_snapshot(snapshot)
    for observed, required in (
        ("host_physical_available_bytes", "min_physical_available_bytes"),
        ("process_available_commit_bytes", "min_process_available_commit_bytes"),
    ):
        if snapshot[observed] < _positive(thresholds[required], required):
            raise ValueError("Memory policy refuses load: " + observed)
    return snapshot


def _no_preloaded_optional():
    if any(name.split(".")[0] in FORBIDDEN_PRELOAD for name in sys.modules):
        raise ValueError("Fresh standalone process required; optional modules already loaded")


def _admit_execution_argument(plan, plan_binding):
    if type(plan_binding) is not dict or set(plan_binding) != {"path", "bytes", "sha256"}:
        raise ValueError("Exact actual plan file binding required")
    data = _read_bound(plan_binding)
    if len(data) > MAX_PLAN or canonical(_json(data)) != canonical(plan):
        raise ValueError("Plan argument differs from actual bound file")


def readonly_preflight(plan, plan_binding):
    """Observe real memory/source policy without loading or creating outputs."""
    _admit_execution_argument(plan, plan_binding)
    validate_plan(plan)
    _, memory = _bootstrap()
    current = validate_snapshot(memory.snapshot())
    result = {
        "contract": CONTRACT,
        "plan": plan_binding,
        "preflight_only": True,
        "passed": False,
        "memory_snapshot": current,
        "memory_preflight": plan["memory_preflight"],
        "error": None,
        "model_load_attempts": 0,
        "faulthandler_enabled_by_this_call": False,
        "execute_output_created": False,
        "claims": dict(CLAIMS),
    }
    try:
        memory_preflight(current, plan["memory_preflight"])
    except ValueError as error:
        result["error"] = str(error)[:2048]
    else:
        result["passed"] = True
    _admit_execution_argument(plan, plan_binding)
    validate_plan(plan)
    return result


def _failure_event(log, phase, error):
    try:
        log.event(phase, "failed", error=error, sample=False)
    except BaseException as sink_error:
        error.add_note("Diagnostic failure persistence also failed: " + str(sink_error))


@contextmanager
def observed_load_context(loader, config, observation, log):
    """Observer failure must never skip an already-entered real context exit."""
    context = loader.load_preloaded(config, observations=observation)
    log.event("load_context_enter", "started")
    try:
        loaded = context.__enter__()
    except BaseException as error:
        _failure_event(log, "load_context_enter", error)
        raise
    error = None
    try:
        log.event("load_context_enter", "returned")
        yield loaded
    except BaseException as body_error:
        error = body_error
        raise
    finally:
        observation_error = None
        try:
            log.event("load_context_exit", "started")
        except BaseException as observer_error:
            observation_error = observer_error
        try:
            suppressed = context.__exit__(
                None if error is None else type(error),
                error,
                None if error is None else error.__traceback__,
            )
            if suppressed:
                raise ValueError("Unexpected diagnostic body suppression")
        except BaseException as close_error:
            _failure_event(log, "load_context_exit", close_error)
            if error is not None:
                error.add_note("Exact context cleanup also failed: " + str(close_error))
            else:
                if observation_error is not None:
                    close_error.add_note(
                        "Exit-start observer also failed: " + str(observation_error)
                    )
                raise
        else:
            try:
                log.event("load_context_exit", "returned")
            except BaseException as observer_error:
                if observation_error is None:
                    observation_error = observer_error
                else:
                    observation_error.add_note(
                        "Exit-end observer also failed: " + str(observer_error)
                    )
            if observation_error is not None:
                if error is not None:
                    error.add_note("Exit observer also failed: " + str(observation_error))
                else:
                    raise observation_error
        finally:
            loaded = None


def execute_plan(plan, plan_binding):
    """Real loader only; never operations injection or inference.

    The native exit/PID termination is observed by the external caller, not
    self-certified by this filesystem report. Native AV cannot be caught here.
    """
    _admit_execution_argument(plan, plan_binding)
    validate_plan(plan)
    _no_preloaded_optional()
    loader, memory = _bootstrap()
    output = output_path(plan["output_directory"])
    output.mkdir(exist_ok=False)
    observation = {}
    report = {
        "contract": CONTRACT,
        "plan": plan_binding,
        "pid": os.getpid(),
        "success": False,
        "exact_load_context_closed": False,
        "source_before": None,
        "source_after": None,
        "sources_unchanged": False,
        "load_observations": observation,
        "error": None,
        "phase_count": 0,
        "scope": {
            "frozen_loader_internal_phases_individually_visible": False,
            "parameter_identity_independently_recomputed_by_diagnostic": False,
            "native_exit_or_process_termination_self_certified": False,
            "external_watchdog_enforced_by_this_cli": False,
            "memory_pressure_proves_crash_cause": False,
            "faulthandler_is_full_native_dump_or_symbolication": False,
        },
        "camera_calls": 0,
        "fused_forward_calls": 0,
        "head_forward_calls": 0,
        "generation_calls": 0,
        "optimizer_attempts": 0,
        "flight_commands": 0,
        "claims": dict(CLAIMS),
    }
    failure = None
    with (
        (output / "native-fault.log").open("xb") as fault,
        (output / "phases.jsonl").open("xb") as events,
    ):
        if faulthandler.is_enabled():
            raise ValueError("Existing faulthandler cannot be silently replaced")
        enabled = False
        phase_log = PhaseLog(events, plan_binding["sha256"], memory.snapshot)
        try:
            faulthandler.enable(file=fault, all_threads=True)
            enabled = True
            with phase_log.phase("memory_preflight"):
                memory_preflight(memory.snapshot(), plan["memory_preflight"])
            with phase_log.phase("source_before"):
                report["source_before"] = inventory(loader._all_files(plan["load_config"]))
                if report["source_before"] != plan["input_inventory"]:
                    raise ValueError("Pre-load declared input drift")
            with observed_load_context(
                loader, plan["load_config"], observation, phase_log
            ) as loaded:
                with phase_log.phase("loaded_context_body"):
                    if (
                        loaded.observations is not observation
                        or (observation.get("load_succeeded") is not True)
                        or observation.get("parameter_identity_before")
                        != (plan["load_config"]["model"]["expected_parameter_sha256"])
                    ):
                        raise ValueError("Frozen exact context did not return verified identity")
            loaded = None
            if observation.get("closed_successfully") is not True or (
                observation.get("source_bindings_unchanged") is not True
            ):
                raise ValueError("Frozen context final identity gates did not pass")
            report["exact_load_context_closed"] = True
            with phase_log.phase("source_after"):
                report["source_after"] = inventory(loader._all_files(plan["load_config"]))
                report["sources_unchanged"] = (
                    report["source_before"] == report["source_after"] == plan["input_inventory"]
                )
                if report["sources_unchanged"] is not True:
                    raise ValueError("Late independently observed input drift")
                for row in plan["source_files"]:
                    _read_bound(row)
                _admit_execution_argument(plan, plan_binding)
                if (
                    hashlib.sha256(canonical(plan["load_config"])).hexdigest()
                    != (plan["load_config_sha256"])
                ):
                    raise ValueError("In-memory exact configuration changed")
            report["success"] = True
        except BaseException as error:
            failure = error
            report["error"] = {"type": type(error).__name__, "message": str(error)[:2048]}
        finally:
            report["phase_count"] = phase_log.count
            if enabled:
                try:
                    faulthandler.disable()
                except BaseException as close_error:
                    report["success"] = False
                    if failure is not None:
                        failure.add_note("faulthandler cleanup also failed: " + str(close_error))
                    else:
                        failure = close_error
                        report["error"] = {
                            "type": type(close_error).__name__,
                            "message": str(close_error)[:2048],
                        }
            fault.flush()
            os.fsync(fault.fileno())
    report["diagnostic_outputs"] = [
        small_record(output / "phases.jsonl"),
        small_record(output / "native-fault.log"),
    ]
    _write_new(output / "report.json", canonical(report) + b"\n", MAX_REPORT)
    if failure is not None:
        raise failure
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit-plan")
    parser.add_argument("--plan")
    parser.add_argument("--plan-sha256")
    parser.add_argument("--output-dir")
    parser.add_argument("--min-physical-available-bytes", type=int)
    parser.add_argument("--min-process-available-commit-bytes", type=int)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.plan:
            if not args.plan_sha256 or args.emit_plan or args.output_dir:
                raise ValueError("Admission requires only explicit plan and SHA")
            plan, binding = admit_plan(args.plan, args.plan_sha256)
            if args.execute:
                result = execute_plan(plan, binding)
                print(
                    json.dumps({"success": result["success"], "report": plan["output_directory"]})
                )
            elif args.preflight_only:
                result = readonly_preflight(plan, binding)
                print(json.dumps(result, allow_nan=False))
                return 0 if result["passed"] else 1
            else:
                print(json.dumps({"admitted": True, "executed": False, "plan": binding}))
        else:
            if args.execute or args.preflight_only or args.plan_sha256 or not args.output_dir:
                raise ValueError("--execute requires an explicitly SHA-bound plan")
            plan = build_plan(
                args.output_dir,
                min_physical_available_bytes=args.min_physical_available_bytes,
                min_process_available_commit_bytes=args.min_process_available_commit_bytes,
            )
            if args.emit_plan:
                print(json.dumps({"executed": False, "plan": emit_plan(args.emit_plan, plan)}))
            else:
                print(canonical(plan).decode())
        return 0
    except Exception as error:
        print(
            json.dumps({"success": False, "type": type(error).__name__, "error": str(error)[:2048]})
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
