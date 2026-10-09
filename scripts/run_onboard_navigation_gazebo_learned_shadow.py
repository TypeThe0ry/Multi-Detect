#!/usr/bin/env python3
"""Exactly loaded learned navigation on new Gazebo camera frames; never flight.

The parent owns a 600-second process-tree watchdog. The child loads the frozen
candidate and saved head before creating an input-only owned simulator. Invalid
model actions and deadline rejections survive in an fsynced append-only journal;
they are observations, not successes or substitute flight commands.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

CAPTURE_CONTRACT = "multidetect.navigation-live-capture-dependencies-v1"
CONTRACT = "multidetect.onboard-navigation-gazebo-learned-shadow-v1"
PINNED_IMAGE = (
    "px4io/px4-sitl-gazebo@sha256:bcf8278e3321948be2266568528107dbfd94eb6fa703171cb013bc4d54dc8ef8"
)
OWNER_LABEL = "multidetect.navigation-live-owner"
PACKAGE_VERSIONS = {"pymavlink": "2.4.50", "fastcrc": "0.5.0", "lxml": "6.1.3"}
FALSE_CLAIMS = dict.fromkeys(
    (
        "quality_claim",
        "candidate_promoted",
        "default_changed",
        "flight_actuated",
        "execution_authority",
        "mission_runner_used",
        "training_performed",
        "text_generation_performed",
        "physical_performance_claimed",
        "hil_real_evidence",
        "outdoor_gnss_evidence",
        "final_release_allowed",
        "training_ready",
    ),
    False,
)


def record(path):
    path = Path(path).resolve(strict=True)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": path.as_posix(), "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def write_new(path, value):
    raw = (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
    with Path(path).open("xb") as stream:
        if stream.write(raw) != len(raw):
            raise OSError("Short durable evidence write")
        stream.flush()
        os.fsync(stream.fileno())


def build_capture_runtime(provider_site=None, python_executable=None):
    """Bind only three missing packages; never insert another venv in sys.path."""
    site = Path(provider_site or ROOT / ".venv-gazebo-navigation-20261008-r1/Lib/site-packages")
    site = site.resolve(strict=True)
    python = Path(python_executable or ROOT / ".venv-vlm-navigation/Scripts/python.exe")
    packages, files, origins = {}, [], {}
    for name, version in PACKAGE_VERSIONS.items():
        package = site / name
        info = site / f"{name}-{version}.dist-info"
        metadata = (info / "METADATA").read_text(encoding="utf-8")
        if f"\nVersion: {version}\n" not in metadata or not package.is_dir():
            raise ValueError("Exact capture package metadata required")
        paths = sorted(
            (
                p
                for directory in (package, info)
                for p in directory.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
            ),
            key=lambda p: p.as_posix(),
        )
        rows = [record(path) for path in paths]
        origins[name] = (package / "__init__.py").resolve(strict=True).as_posix()
        packages[name] = {"version": version, "root": package.as_posix(), "files": rows}
        files.extend(rows)
    return {
        "contract": CAPTURE_CONTRACT,
        "python_executable": python.resolve(strict=True).as_posix(),
        "provider_site": site.as_posix(),
        "packages": packages,
        "source_files": sorted(files, key=lambda row: row["path"]),
        "module_origins": origins,
        "no_sys_path_insertion": True,
        "allowed_fallback_top_level_names": sorted(PACKAGE_VERSIONS),
        "shadow_existing_modules": False,
    }


class _CapturePackageFinder(importlib.abc.MetaPathFinder):
    def __init__(self, runtime):
        self.runtime = runtime

    def find_spec(self, fullname, path=None, target=None):
        if path is not None or fullname not in self.runtime["packages"]:
            return None
        spec = importlib.machinery.PathFinder.find_spec(
            fullname,
            [self.runtime["provider_site"]],
            target,
        )
        if (
            spec is None
            or Path(spec.origin).resolve().as_posix() != (self.runtime["module_origins"][fullname])
        ):
            raise ImportError("Bound capture package origin differs")
        return spec


@contextmanager
def scoped_capture_dependencies(runtime, observations):
    if runtime.get("contract") != CAPTURE_CONTRACT:
        raise ValueError("Explicit capture dependency contract required")
    if Path(sys.executable).resolve().as_posix() != runtime["python_executable"]:
        raise ValueError("Capture dependency interpreter differs")
    if (
        runtime["no_sys_path_insertion"] is not True
        or runtime["shadow_existing_modules"] is not False
        or runtime["allowed_fallback_top_level_names"] != sorted(PACKAGE_VERSIONS)
        or set(runtime["packages"]) != set(PACKAGE_VERSIONS)
    ):
        raise ValueError("Only three exact non-shadowing capture fallbacks allowed")
    for name in PACKAGE_VERSIONS:
        if runtime["packages"][name]["version"] != PACKAGE_VERSIONS[name]:
            raise ValueError("Exact capture dependency versions required")
        if name in sys.modules or importlib.util.find_spec(name) is not None:
            raise ValueError("Capture fallback must not shadow an existing package")
    before = [record(row["path"]) for row in runtime["source_files"]]
    if before != runtime["source_files"]:
        raise ValueError("Capture dependency files changed")
    paths_before = tuple(sys.path)
    finder = _CapturePackageFinder(runtime)
    sys.meta_path.insert(0, finder)
    observations.update({"sys_path_modified": False, "fallback_installed": True})
    try:
        yield
    finally:
        try:
            sys.meta_path.remove(finder)
            observations["fallback_restored"] = finder not in sys.meta_path
            observations["sys_path_unchanged"] = tuple(sys.path) == paths_before
            observations["source_files_after"] = [record(row["path"]) for row in before]
            observations["source_bindings_unchanged"] = observations["source_files_after"] == before
            observations["imported_module_origins"] = {
                name: Path(module.__file__).resolve().as_posix()
                for name, module in sys.modules.items()
                if name in PACKAGE_VERSIONS and getattr(module, "__file__", None)
            }
            for name, origin in observations["imported_module_origins"].items():
                if origin != runtime["module_origins"][name]:
                    raise ValueError("Actual capture package origin differs")
            if (
                not observations["sys_path_unchanged"]
                or not observations["source_bindings_unchanged"]
            ):
                raise ValueError("Capture dependency scope changed")
        except BaseException as exc:
            observations["cleanup_error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise


class ShadowRequestSink:
    """Single owner, no retries, with durable rejected raw observations."""

    def __init__(self, backend, journal, *, clock, max_requests=8):
        self.backend, self.journal, self.clock = backend, Path(journal), clock
        self.max_requests, self.rows, self.seen = max_requests, [], set()

    def __call__(self, prepared):
        from scripts.onboard_navigation_inference_backend import NavigationBackendRejected

        request = prepared.request
        key = (request.generation, request.canonical_hash)
        if len(self.rows) >= self.max_requests or key in self.seen:
            raise ValueError("Duplicate or excess shadow inference request")
        if request.generation != len(self.rows) + 1:
            raise ValueError("Shadow generations must advance without retries")
        self.seen.add(key)
        row = {
            "generation": request.generation,
            "request_sha256": key[1],
            "actor_identity_sha256": request.actor_identity_sha256,
            "source_binding_sha256": request.source_binding_sha256,
            "source_frame_count": len(request.source_frames),
            "submitted_at_s": request.submitted_at_s,
            "deadline_s": request.deadline_s,
            "max_frame_age_s": request.max_frame_age_s,
            "started_callback_at_s": self.clock(),
            "accepted": False,
            "flight_commands_sent": 0,
            "retry_performed": False,
        }
        try:
            answer = self.backend.infer(request)
            row.update(
                {
                    "accepted": True,
                    "result": answer["learned_readout_result"].to_mapping(),
                    "raw_prediction": answer["raw_prediction"],
                    "metrics": answer["metrics"],
                }
            )
        except NavigationBackendRejected as exc:
            diagnostics = json.loads(exc.diagnostics_json)
            row.update(
                {
                    "rejection": {"type": type(exc).__name__, "message": str(exc)},
                    "metrics": diagnostics,
                    "raw_prediction": diagnostics.get("raw_prediction"),
                }
            )
        row["callback_completed_at_s"] = self.clock()
        row["host_callback_elapsed_ms"] = (
            row["callback_completed_at_s"] - row["started_callback_at_s"]
        ) * 1000
        raw = (json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n").encode()
        # One file owner; xb for the first line, ab only after this instance wrote it.
        with self.journal.open("xb" if not self.rows else "ab") as stream:
            if stream.write(raw) != len(raw):
                raise OSError("Short raw shadow observation write")
            stream.flush()
            os.fsync(stream.fileno())
        self.rows.append(row)
        print(
            json.dumps(
                {
                    "event": "live_shadow_observation",
                    "generation": request.generation,
                    "accepted": row["accepted"],
                    "raw_prediction": row["raw_prediction"],
                    "rejection": row.get("rejection"),
                    "host_callback_elapsed_ms": row["host_callback_elapsed_ms"],
                }
            ),
            flush=True,
        )


def owned_docker_runner(claim_path, container_name, ownership_intent=None):
    def run(argv, *, timeout_s):
        effective = list(argv)
        launching = len(argv) > 1 and argv[1] == "run"
        if launching:
            if "--name" not in argv or argv[argv.index("--name") + 1] != container_name:
                raise ValueError("Unexpected owned container launch")
            if ownership_intent is not None:
                if PINNED_IMAGE not in argv or "--cidfile" in argv:
                    raise ValueError("Pinned image and fresh native cidfile required")
                cidfile = Path(claim_path).with_name("native-container.cid")
                if cidfile.exists():
                    raise FileExistsError("Owned native cidfile already exists")
                effective[2:2] = [
                    "--label",
                    OWNER_LABEL + "=" + ownership_intent["token"],
                    "--cidfile",
                    str(cidfile),
                ]
        result = subprocess.run(
            effective,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
        if launching and result.returncode == 0:
            container_id = result.stdout.strip()
            if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
                raise ValueError("Exact native Docker ID required for durable ownership")
            write_new(
                claim_path,
                {
                    "pid": os.getpid(),
                    "container_name": container_name,
                    "container_id": container_id,
                    "launch_native_exit_code": 0,
                    "launch_command": effective,
                    "ownership_token": None
                    if ownership_intent is None
                    else ownership_intent["token"],
                },
            )
        return result

    return run


def worker(plan_path, plan_sha256):
    from scripts import onboard_navigation_live_shadow_plan as plans

    plan, binding = plans.admit_plan(plan_path, plan_sha256)
    output = Path(plan["output_dir"])
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "contract": CONTRACT,
        "pid": os.getpid(),
        "success": False,
        "native_exit_code": 1,
        "plan": binding,
        "claims": dict(FALSE_CLAIMS),
        "loader": {},
        "capture_dependencies": {},
        "observations": [],
        "source_bindings_before": None,
    }
    error = None
    try:
        report["source_bindings_before"] = plans.capture_sources(plan)
        intent_path = output.with_name(output.name + "-ownership.json")
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
        if (
            intent.get("plan_sha256") != plan_sha256
            or intent.get("image") != PINNED_IMAGE
            or re.fullmatch(r"[0-9a-f]{32}", intent.get("token", "")) is None
        ):
            raise ValueError("Supervisor ownership intent differs")
        report["ownership_intent"] = record(intent_path)
        # Import-quiet admission is complete before optional model imports.
        from scripts.onboard_navigation_live_loader import load_preloaded

        with load_preloaded(plan["load_config"], observations=report["loader"]) as loaded:
            print(json.dumps({"event": "exact_live_model_loaded", "pid": os.getpid()}), flush=True)
            with scoped_capture_dependencies(
                plan["capture_runtime"], report["capture_dependencies"]
            ):
                from scripts import run_gazebo_navigation_input_capture as capture
                from scripts import run_simulator_docker_acceptance as original
                from scripts.onboard_navigation_gazebo_shadow_input import (
                    GazeboNavigationInputBuffer,
                )
                from scripts.onboard_navigation_learned_readout_backend import (
                    PreloadedLearnedNavigationBackend,
                )

                service, sink = None, None

                def make_buffer(prompt, **kwargs):
                    nonlocal service, sink
                    if service is not None:
                        raise RuntimeError("Only one capture clock/backend allowed")
                    service = PreloadedLearnedNavigationBackend(
                        loaded.engine,
                        loaded.engine_identity,
                        loaded.head,
                        loaded.normalization,
                        loaded.readout_identity,
                        loaded.readout_metadata,
                        clock=kwargs["clock"],
                    )
                    sink = ShadowRequestSink(
                        service, output / "observations.jsonl", clock=kwargs["clock"]
                    )
                    report["observations"] = sink.rows
                    report["clock_identity"] = kwargs["host_clock_identity"]
                    return GazeboNavigationInputBuffer(prompt, **kwargs)

                cli = owned_docker_runner(
                    output / "owned-container.json",
                    capture.CONTAINER_NAME,
                    intent,
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
                report["capture_input_report"] = record(output / "capture/input-report.json")
                report["capture_success"] = result["success"]
                report["observations"] = sink.rows if sink is not None else []
                if not result["success"] or len(report["observations"]) != options["sensor_frames"]:
                    raise ValueError("Every precommitted fresh camera request must close")
        report["source_bindings_after"] = plans.capture_sources(plan)
        if report["source_bindings_before"] != report["source_bindings_after"]:
            raise ValueError("Live source/runtime bindings changed")
        report["source_bindings_unchanged"] = True
        report["success"], report["native_exit_code"] = True, 0
    except BaseException as exc:
        error = exc
        report["error"] = {"type": type(exc).__name__, "message": str(exc)[:2048]}
    finally:
        report["accepted_count"] = sum(row["accepted"] for row in report["observations"])
        report["rejected_count"] = len(report["observations"]) - report["accepted_count"]
        report["model_output_quality_passed"] = False
        if (output / "observations.jsonl").is_file():
            report["raw_journal"] = record(output / "observations.jsonl")
        write_new(output / "worker-report.json", report)
    if error is not None and not isinstance(error, Exception):
        raise error
    return report["native_exit_code"]


def docker_inspect(docker, name):
    result = subprocess.run(
        [docker, "container", "inspect", name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )
    if result.returncode:
        if "No such" in result.stderr:
            return None
        raise RuntimeError("Docker inspect failed: " + result.stderr[:1024])
    rows = json.loads(result.stdout)
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("Exactly one Docker inspect record required")
    return rows[0]


def terminate_owned_tree(process):
    if process.poll() is not None:
        return None
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        process.wait(timeout=15)
        return {
            "command_scope": "only_the_launched_worker_process_tree",
            "pid": process.pid,
            "native_exit_code": result.returncode,
        }
    process.kill()
    process.wait(timeout=15)
    return {"command_scope": "only_the_launched_worker", "pid": process.pid}


def observe_owned_cleanup(docker, name, output, report):
    """Only a durably claimed, matching native container ID may be removed."""
    try:
        current = docker_inspect(docker, name)
        claim = output / "owned-container.json"
        if current is not None and not claim.is_file():
            intent_path = output.with_name(output.name + "-ownership.json")
            if intent_path.is_file():
                intent = json.loads(intent_path.read_text(encoding="utf-8"))
                config = current.get("Config") or {}
                token = intent.get("token", "")
                if (
                    re.fullmatch(r"[0-9a-f]{32}", token) is not None
                    and current.get("Name") == "/" + name
                    and intent.get("container_name") == name
                    and intent.get("image") == PINNED_IMAGE
                    and config.get("Image") == PINNED_IMAGE
                    and (config.get("Labels") or {}).get(OWNER_LABEL) == token
                ):
                    write_new(
                        claim,
                        {
                            "container_name": name,
                            "container_id": current["Id"],
                            "ownership_token": token,
                            "ownership_recovered_from_exact_label_image_name": True,
                            "launch_native_exit_code": None,
                        },
                    )
                    report["durable_ownership_recovered"] = True
        if current is not None and claim.is_file():
            ownership = json.loads(claim.read_text(encoding="utf-8"))
            identity = ownership.get("container_id")
            if (
                ownership.get("container_name") == name
                and re.fullmatch(r"[0-9a-f]{64}", identity or "") is not None
                and current.get("Id") == identity
            ):
                codes = []
                errors = []
                for arguments in (["stop", "--timeout", "2", identity], ["rm", identity]):
                    try:
                        done = subprocess.run(
                            [docker, *arguments], capture_output=True, timeout=15, check=False
                        )
                        codes.append(done.returncode)
                        if done.returncode != 0:
                            errors.append(
                                {"command": arguments, "native_exit_code": done.returncode}
                            )
                    except Exception as exc:
                        codes.append(None)
                        errors.append(
                            {
                                "command": arguments,
                                "type": type(exc).__name__,
                                "message": str(exc)[:2048],
                            }
                        )
                report["outer_owned_cleanup_exit_codes"] = codes
                report["outer_owned_cleanup_errors"] = errors
            else:
                report["container_identity_mismatch_not_touched"] = True
        elif current is not None:
            report["unclaimed_container_not_touched"] = True
        report["container_absent_after"] = docker_inspect(docker, name) is None
    except Exception as exc:
        report["container_absent_after"] = False
        report["outer_cleanup_error"] = {"type": type(exc).__name__, "message": str(exc)[:2048]}


def supervise(plan_path, plan_sha256):
    from scripts import onboard_navigation_live_shadow_plan as plans
    from scripts.run_gazebo_navigation_input_capture import CONTAINER_NAME

    plan, binding = plans.admit_plan(plan_path, plan_sha256)
    output = Path(plan["output_dir"])
    if output.exists() or plan["worker_timeout_s"] != 600:
        raise ValueError("Fresh output and fixed 600-second watchdog required")
    docker = shutil.which("docker")
    if not docker or docker_inspect(docker, CONTAINER_NAME) is not None:
        raise ValueError("Docker ready and owned capture name absent required")
    report_path = output.with_name(output.name + "-supervisor.json")
    intent_path = output.with_name(output.name + "-ownership.json")
    stdout_path, stderr_path = output.with_suffix(".stdout.log"), output.with_suffix(".stderr.log")
    for path in (report_path, intent_path, stdout_path, stderr_path):
        if path.exists():
            raise FileExistsError("Fresh supervisor outputs required")
    output.parent.mkdir(parents=True, exist_ok=True)
    write_new(
        intent_path,
        {
            "plan_sha256": plan_sha256,
            "container_name": CONTAINER_NAME,
            "image": PINNED_IMAGE,
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
        "claims": dict(FALSE_CLAIMS),
        "container_absent_before": True,
        "ownership_intent": record(intent_path),
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
                        "event": "live_shadow_worker_started",
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
                report["termination"] = terminate_owned_tree(child)
                report["worker_exit_code"] = child.returncode
    except BaseException as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)[:2048]}
        if child is not None:
            try:
                report["termination"] = terminate_owned_tree(child)
            except Exception as termination_error:
                report["termination_error"] = {
                    "type": type(termination_error).__name__,
                    "message": str(termination_error)[:2048],
                }
    finally:
        report["worker_wait_completed"] = child is not None and child.poll() is not None
        observe_owned_cleanup(docker, CONTAINER_NAME, output, report)
        try:
            if (output / "worker-report.json").is_file():
                report["worker_report"] = record(output / "worker-report.json")
                closed = json.loads((output / "worker-report.json").read_text(encoding="utf-8"))
                report["success"] = (
                    report.get("worker_exit_code") == 0
                    and closed["success"] is True
                    and report["worker_wait_completed"]
                    and report["container_absent_after"]
                    and not report["watchdog_fired"]
                    and not report.get("outer_owned_cleanup_errors")
                    and "outer_cleanup_error" not in report
                    and "error" not in report
                    and "termination_error" not in report
                )
            report["stdout"], report["stderr"] = record(stdout_path), record(stderr_path)
        except Exception as exc:
            report["success"] = False
            report["final_validation_error"] = {"type": type(exc).__name__, "message": str(exc)}
        write_new(report_path, report)
    print(
        json.dumps(
            {
                "event": "live_shadow_supervisor_closed",
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
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args(argv)
    return (worker if args.worker else supervise)(args.plan, args.plan_sha256)


if __name__ == "__main__":
    raise SystemExit(main())
