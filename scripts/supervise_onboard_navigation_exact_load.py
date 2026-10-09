"""Owned, opt-in exact-load supervision; no inference, training or flight.

Default mode runs only a fresh readonly preflight child. One absolute 600-second
deadline covers both children. An unnamed Windows Job owns each child tree;
the child waits for assignment before executing the frozen diagnostic entry.
Small sources are hashed, large model files are stat-observed, as in the plan.
Native exit and owned-process cleanup are observations, not model quality gates.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import json
import math
import os
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = "scripts/diagnose_onboard_navigation_exact_load.py"
ENTRY_SHA = "2567e9271ba2fb344680470050b3a39f0f0ae347ba1d32a1e1e89a54cff112b3"
NAV = ".venv-vlm-navigation/Scripts/python.exe"
NAV_SHA = "137c474a645a9471ab3a369084e7cd573cd8b7ff8f81cb85363fffdf5904106c"
FILES = (
    "scripts/supervise_onboard_navigation_exact_load.py",
    "tests/test_supervise_onboard_navigation_exact_load.py",
)
CONTRACT = "multidetect.navigation-owned-exact-load-supervisor-v1"
TIMEOUT_S = 600
CAPTURE_CAP = 1024 * 1024
BOOTSTRAP = (
    "import sys,runpy,json,os; "
    "print('__OWNED_WORKER__'+json.dumps({'pid':os.getpid(),'ppid':os.getppid(),"
    "'python_executable':sys.executable,'base_executable':sys._base_executable,"
    "'argv':sys.argv[1:]}),flush=True); "
    "gate=sys.stdin.buffer.read(1); "
    "assert gate==b'G', 'Owned supervisor did not release child'; "
    "import site; site.main(); "
    "sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')"
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def record(path, *, cap=2 * 1024 * 1024):
    path = Path(path).resolve(strict=True)
    before = path.stat()
    if not path.is_file() or not 0 <= before.st_size <= cap:
        raise ValueError("Bounded regular file required")
    raw = path.read_bytes()
    after = path.stat()

    def identity(stat):
        return (stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino)

    if identity(before) != identity(after) or len(raw) != after.st_size:
        raise ValueError("File changed during bound read")
    return {"path": path.as_posix(), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def write_new(path, raw):
    with Path(path).open("xb") as stream:
        if stream.write(raw) != len(raw):
            raise OSError("Short supervisor evidence write")
        stream.flush()
        os.fsync(stream.fileno())
    row = record(path)
    if row["bytes"] != len(raw) or row["sha256"] != hashlib.sha256(raw).hexdigest():
        raise ValueError("Supervisor evidence readback differs")
    return row


def diagnostic_api():
    entry = (ROOT / ENTRY).resolve(strict=True)
    if record(entry)["sha256"] != ENTRY_SHA:
        raise ValueError("Frozen diagnostic entry changed")
    spec = importlib.util.spec_from_file_location("_owned_exact_load_diagnostic", entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def admit(plan_path, digest):
    api = diagnostic_api()
    plan, binding = api.admit_plan(plan_path, digest)
    runtime = plan["load_config"]["runtime"]
    nav = (ROOT / NAV).resolve(strict=True)
    nav_record = record(nav)
    if (
        Path(runtime["python_executable"]).resolve(strict=True) != nav
        or nav_record != runtime["python_binding"]
        or nav_record["sha256"] != NAV_SHA
    ):
        raise ValueError("Fixed Navigation interpreter binding differs")
    own_sources = [record(ROOT / name) for name in FILES]
    return api, plan, binding, nav_record, own_sources


def output_path(value, diagnostic_output):
    path = Path(value).resolve(strict=False)
    parent = (ROOT / "artifacts/diagnostics").resolve(strict=True)
    if (
        path.parent != parent
        or not path.name.startswith("onboard-navigation-exact-load-")
        or "-supervisor-" not in path.name
        or path == Path(diagnostic_output).resolve(strict=False)
        or path.exists()
    ):
        raise ValueError("Fresh direct-child supervisor output required")
    return path


class WindowsJob:
    """A new unnamed job; never enumerates or terminates unrelated processes."""

    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows-owned Job supervision required")
        from ctypes import wintypes as w

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("ProcessTime", ctypes.c_int64),
                ("JobTime", ctypes.c_int64),
                ("Flags", w.DWORD),
                ("MinWS", ctypes.c_size_t),
                ("MaxWS", ctypes.c_size_t),
                ("ActiveLimit", w.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("Priority", w.DWORD),
                ("Scheduling", w.DWORD),
            ]

        class IO(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "ReadCount",
                    "WriteCount",
                    "OtherCount",
                    "ReadBytes",
                    "WriteBytes",
                    "OtherBytes",
                )
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("Basic", BasicLimits),
                ("IO", IO),
                ("ProcessMemory", ctypes.c_size_t),
                ("JobMemory", ctypes.c_size_t),
                ("PeakProcessMemory", ctypes.c_size_t),
                ("PeakJobMemory", ctypes.c_size_t),
            ]

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("UserTime", ctypes.c_int64),
                ("KernelTime", ctypes.c_int64),
                ("PeriodUserTime", ctypes.c_int64),
                ("PeriodKernelTime", ctypes.c_int64),
                ("PageFaults", w.DWORD),
                ("TotalProcesses", w.DWORD),
                ("ActiveProcesses", w.DWORD),
                ("TerminatedProcesses", w.DWORD),
            ]

        self.accounting = Accounting
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        declarations = {
            "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
            "QueryInformationJobObject": (
                [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p],
                w.BOOL,
            ),
            "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
            "CloseHandle": ([w.HANDLE], w.BOOL),
            "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "IsProcessInJob": ([w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL),
            "QueryFullProcessImageNameW": (
                [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)],
                w.BOOL,
            ),
        }
        for name, (args, result) in declarations.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
        if not self.kernel.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def resume(self, process):
        # Popen closes the initial thread handle; resume only this new owned process.
        from ctypes import wintypes as w

        resume = ctypes.WinDLL("ntdll").NtResumeProcess
        resume.argtypes, resume.restype = [w.HANDLE], ctypes.c_long
        status = resume(int(process._handle))
        if status != 0:
            raise OSError("Owned suspended process resume failed: " + hex(status & 0xFFFFFFFF))

    def active(self):
        value = self.accounting()
        if not self.kernel.QueryInformationJobObject(
            self.handle, 1, ctypes.byref(value), ctypes.sizeof(value), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return value.ActiveProcesses

    def admit_worker(self, process, identity, command):
        """Authenticate the launcher/worker distinction before releasing Python site."""
        from ctypes import wintypes as w

        if (
            type(identity) is not dict
            or set(identity) != {"pid", "ppid", "python_executable", "base_executable", "argv"}
            or type(identity["pid"]) is not int
            or identity["pid"] <= 0
            or type(identity["ppid"]) is not int
            or identity["argv"] != command[command.index(BOOTSTRAP) + 1 :]
            or Path(identity["python_executable"]).resolve(strict=True)
            != Path(command[0]).resolve(strict=True)
        ):
            raise ValueError("Fixed bootstrap worker identity differs")

        def image(handle):
            buffer = ctypes.create_unicode_buffer(32768)
            size = w.DWORD(len(buffer))
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            return Path(buffer.value).resolve(strict=True)

        class ProcessInfo(ctypes.Structure):
            _fields_ = [
                ("Reserved", ctypes.c_void_p),
                ("Peb", ctypes.c_void_p),
                ("Reserved2", ctypes.c_void_p * 2),
                ("Pid", ctypes.c_size_t),
                ("ParentPid", ctypes.c_size_t),
            ]

        if image(int(process._handle)) != Path(command[0]).resolve(strict=True):
            raise ValueError("Actual launcher image differs")
        handle = self.kernel.OpenProcess(0x1101, False, identity["pid"])
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ntdll = ctypes.WinDLL("ntdll")
            query = ntdll.NtQueryInformationProcess
            query.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.ULONG, ctypes.c_void_p]
            query.restype = ctypes.c_long
            info = ProcessInfo()
            status = query(handle, 0, ctypes.byref(info), ctypes.sizeof(info), None)
            if status != 0:
                raise OSError("Native process identity query failed: " + hex(status & 0xFFFFFFFF))
            worker_image = image(handle)
            if (
                info.Pid != identity["pid"]
                or info.ParentPid != identity["ppid"]
                or (identity["pid"] != process.pid and info.ParentPid != process.pid)
                or (identity["pid"] == process.pid and info.ParentPid != os.getpid())
                or worker_image != Path(identity["base_executable"]).resolve(strict=True)
            ):
                raise ValueError("Actual worker parent/image differs from fixed handshake")
            member = w.BOOL()
            if not self.kernel.IsProcessInJob(handle, self.handle, ctypes.byref(member)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not member.value:
                raise ValueError("Atomic-start worker did not inherit the owned Job")
            return {
                "identity": identity,
                "actual_image": worker_image.as_posix(),
                "actual_parent_pid": info.ParentPid,
                "job_membership_verified": True,
                "worker_explicitly_assigned": False,
            }
        finally:
            if not self.kernel.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())

    def terminate(self):
        if not self.kernel.TerminateJobObject(self.handle, 124):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            if not self.kernel.CloseHandle(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = None


def run_child(command, output, label, deadline, *, owner_factory=WindowsJob, clock=time.monotonic):
    overflow = threading.Event()
    identity_ready = threading.Event()
    identities = []
    errors, threads = [], []
    child = None
    owner = owner_factory()
    row = {
        "phase": label,
        "command": command,
        "pid": None,
        "launcher_pid": None,
        "worker_pid": None,
        "worker_admitted_before_release": False,
        "launcher_started_suspended": False,
        "native_exit_code": None,
        "watchdog_fired": False,
        "capture_overflow": False,
        "owned_tree_empty": False,
        "ownership_assigned_before_release": False,
    }
    streams = []

    def drain(pipe, stream, *, worker_header=False):
        used = 0
        header = b""
        try:
            while data := pipe.read1(4096):
                saved = data[: max(0, CAPTURE_CAP - used)]
                if stream.write(saved) != len(saved):
                    raise OSError("Short subprocess capture write")
                used += len(saved)
                if len(saved) != len(data):
                    overflow.set()
                if worker_header and not identity_ready.is_set():
                    header += data
                    if len(header) > 16384:
                        raise ValueError("Bounded worker handshake exceeded")
                    if b"\n" in header:
                        line = header.split(b"\n", 1)[0].rstrip(b"\r")
                        if not line.startswith(b"__OWNED_WORKER__"):
                            raise ValueError("Fixed worker handshake missing")
                        identities.append(json.loads(line[len(b"__OWNED_WORKER__") :]))
                        identity_ready.set()
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException as error:
            errors.append(error)
            overflow.set()
        finally:
            pipe.close()

    try:
        if clock() >= deadline:
            raise TimeoutError("Absolute supervisor deadline exhausted before spawn")
        for name in ("stdout", "stderr"):
            streams.append((output / f"{label}-{name}.log").open("xb"))
        child = subprocess.Popen(
            command,
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | 0x00000004,
        )
        row["pid"] = child.pid
        row["launcher_pid"] = child.pid
        row["launcher_started_suspended"] = True  # CREATE_SUSPENDED; no launcher code yet.
        owner.assign(child)
        row["ownership_assigned_before_release"] = True
        for index, (pipe, stream) in enumerate(
            zip((child.stdout, child.stderr), streams, strict=True)
        ):
            thread = threading.Thread(
                target=drain, args=(pipe, stream), kwargs={"worker_header": index == 0}, daemon=True
            )
            thread.start()
            threads.append(thread)
        owner.resume(child)
        while not identity_ready.is_set():
            if clock() >= deadline:
                row["watchdog_fired"] = True
                raise TimeoutError("Worker identity handshake deadline exhausted")
            if overflow.is_set() or child.poll() is not None:
                raise ValueError("Worker identity handshake did not complete")
            time.sleep(0.01)
        row["worker_provenance"] = owner.admit_worker(child, identities[0], command)
        row["worker_pid"] = identities[0]["pid"]
        row["worker_admitted_before_release"] = True
        child.stdin.write(b"G")
        child.stdin.flush()
        child.stdin.close()
        while child.poll() is None:
            if clock() >= deadline or overflow.is_set():
                row["watchdog_fired"] = clock() >= deadline
                owner.terminate()
                break
            time.sleep(0.02)
        row["native_exit_code"] = child.wait(timeout=5)
        if owner.active():
            row["descendants_survived_native_exit"] = True
            owner.terminate()
        until = time.monotonic() + 5
        while owner.active() and time.monotonic() < until:
            time.sleep(0.01)
        row["owned_tree_empty"] = owner.active() == 0
    except BaseException as error:
        row["error"] = {"type": type(error).__name__, "message": str(error)[:2048]}
        if child is not None:
            if row["ownership_assigned_before_release"]:
                owner.terminate()
            else:
                child.kill()  # Still waiting on handshake; cannot have executed the entry.
            row["native_exit_code"] = child.wait(timeout=5)
            row["owned_tree_empty"] = owner.active() == 0
    finally:
        try:
            owner.close()
        except BaseException as error:
            row["job_close_error"] = {"type": type(error).__name__, "message": str(error)[:2048]}
        finally:
            if child is not None and child.stdin is not None and not child.stdin.closed:
                # A refused, unassigned bootstrap receives EOF, never G.
                child.stdin.close()
            for thread in threads:
                thread.join(timeout=5)
            if child is not None:
                for pipe in (child.stdin, child.stdout, child.stderr):
                    if pipe is not None and not pipe.closed:
                        pipe.close()
            for stream in streams:
                stream.close()
    row["capture_overflow"] = overflow.is_set()
    row["deadline_exhausted"] = clock() >= deadline
    row["capture_threads_closed"] = all(not thread.is_alive() for thread in threads)
    if errors:
        row["capture_errors"] = [type(error).__name__ + ":" + str(error)[:1024] for error in errors]
    row["captures"] = [record(output / f"{label}-{name}.log") for name in ("stdout", "stderr")]
    return row


def child_complete(row):
    return (
        type(row["native_exit_code"]) is int
        and row["native_exit_code"] == 0
        and type(row["pid"]) is int
        and row["pid"] > 0
        and type(row["worker_pid"]) is int
        and row["worker_pid"] > 0
        and row["worker_admitted_before_release"] is True
        and row.get("launcher_started_suspended") is True
        and row.get("worker_provenance", {}).get("job_membership_verified") is True
        and row["owned_tree_empty"] is True
        and row["ownership_assigned_before_release"] is True
        and row["watchdog_fired"] is False
        and row["capture_overflow"] is False
        and row["capture_threads_closed"] is True
        and row.get("deadline_exhausted", False) is False
        and not row.get("error")
        and not row.get("capture_errors")
        and not row.get("job_close_error")
        and not row.get("descendants_survived_native_exit")
    )


def verify_preflight(api, row, plan, binding):
    if not child_complete(row):
        raise ValueError("Readonly preflight child did not close cleanly")
    if any(record(item["path"]) != item for item in row["captures"]):
        raise ValueError("Preflight capture changed before verification")
    stdout = Path(row["captures"][0]["path"]).read_bytes().split(b"\n", 1)[1]
    result = api._json(stdout)
    if (
        type(result) is not dict
        or set(result)
        != {
            "contract",
            "plan",
            "preflight_only",
            "passed",
            "memory_snapshot",
            "memory_preflight",
            "error",
            "model_load_attempts",
            "faulthandler_enabled_by_this_call",
            "execute_output_created",
            "claims",
        }
        or result.get("contract") != api.CONTRACT
        or result.get("plan") != binding
        or result.get("preflight_only") is not True
        or result.get("passed") is not True
        or type(result.get("model_load_attempts")) is not int
        or result["model_load_attempts"] != 0
        or result.get("faulthandler_enabled_by_this_call") is not False
        or result.get("execute_output_created") is not False
        or result.get("error") is not None
        or canonical(result.get("claims")) != canonical(api.CLAIMS)
        or canonical(result.get("memory_preflight")) != canonical(plan["memory_preflight"])
    ):
        raise ValueError("Readonly preflight result is incomplete or refused")
    snapshot = result["memory_snapshot"]
    if (
        type(snapshot) is not dict
        or type(snapshot.get("pid")) is not int
        or snapshot["pid"] != row["worker_pid"]
    ):
        raise ValueError("Preflight snapshot does not belong to owned child")
    for counter in (
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
        if type(snapshot.get(counter)) is not int or snapshot[counter] < 0:
            raise ValueError("Incomplete child memory observation")
    observed_at = snapshot.get("observed_at_unix_s")
    if (
        type(observed_at) not in (int, float)
        or not math.isfinite(observed_at)
        or observed_at <= 0
        or snapshot.get("continuous_sampling") is not False
        or snapshot.get("GPU_memory_measured") is not False
        or snapshot.get("os_peak_scope") != "process_lifetime"
        or snapshot["host_physical_available_bytes"] > snapshot["host_physical_total_bytes"]
        or snapshot["process_available_commit_bytes"]
        > snapshot["process_limited_commit_limit_bytes"]
        or snapshot["process_working_set_bytes"] > snapshot["process_peak_working_set_bytes"]
    ):
        raise ValueError("Child memory scope/counters inconsistent")
    for counter, threshold in (
        ("host_physical_available_bytes", "min_physical_available_bytes"),
        ("process_available_commit_bytes", "min_process_available_commit_bytes"),
    ):
        if snapshot[counter] < plan["memory_preflight"][threshold]:
            raise ValueError("Child did not meet declared memory policy")
    if Path(plan["output_directory"]).exists():
        raise ValueError("Readonly preflight created execute output")
    return result


def final_sources(api, plan, binding, nav_binding, own_sources):
    api._read_bound(binding)
    for source in plan["source_files"]:
        api._read_bound(source)
    if record(ROOT / NAV) != nav_binding or [record(ROOT / name) for name in FILES] != own_sources:
        raise ValueError("Supervisor/interpreter source drift")
    if record(ROOT / ENTRY)["sha256"] != ENTRY_SHA:
        raise ValueError("Final frozen entry drift")
    loader, _ = api._bootstrap()
    current = api.inventory(loader._all_files(plan["load_config"]))
    if current != plan["input_inventory"]:
        raise ValueError("Final independently observed source inventory drift")
    return current


def verify_load_report(api, row, plan, binding):
    if not child_complete(row):
        raise ValueError("Exact-load native child did not close cleanly")
    path = Path(plan["output_directory"]) / "report.json"
    report_binding = record(path)
    report = api._json(path.read_bytes())
    if (
        report.get("contract") != api.CONTRACT
        or report.get("plan") != binding
        or report.get("pid") != row["worker_pid"]
        or report.get("success") is not True
        or report.get("exact_load_context_closed") is not True
        or report.get("sources_unchanged") is not True
        or report.get("error") is not None
        or report.get("source_before") != plan["input_inventory"]
        or report.get("source_after") != plan["input_inventory"]
        or canonical(report.get("claims")) != canonical(api.CLAIMS)
    ):
        raise ValueError("Exact-load report final gates failed")
    observation = report["load_observations"]
    if (
        observation.get("load_succeeded") is not True
        or observation.get("closed_successfully") is not True
        or observation.get("source_bindings_unchanged") is not True
        or observation.get("parameter_identity_before")
        != plan["load_config"]["model"]["expected_parameter_sha256"]
        or observation.get("parameter_identity_after")
        != plan["load_config"]["model"]["expected_parameter_sha256"]
    ):
        raise ValueError("Exact-load parameter/cleanup declaration failed")
    for name in (
        "camera_calls",
        "fused_forward_calls",
        "head_forward_calls",
        "generation_calls",
        "optimizer_attempts",
        "flight_commands",
    ):
        if type(report.get(name)) is not int or report[name] != 0:
            raise ValueError("Diagnostic performed out-of-scope work")
    for payload in report["diagnostic_outputs"]:
        if (
            Path(payload["path"]).resolve().parent != path.parent.resolve()
            or record(payload["path"]) != payload
        ):
            raise ValueError("Diagnostic output binding differs")
    if record(path) != report_binding:
        raise ValueError("Diagnostic report changed during verification")
    return report_binding


def supervise(plan_path, digest, output_directory, *, execute=False):
    started = time.monotonic()
    api, plan, binding, nav_binding, own_sources = admit(plan_path, digest)
    output = output_path(output_directory, plan["output_directory"])
    output.mkdir(exist_ok=False)
    result = {
        "contract": CONTRACT,
        "plan": binding,
        "interpreter": nav_binding,
        "supervisor_sources": own_sources,
        "mode": "execute" if execute else "preflight_only",
        "absolute_timeout_s": TIMEOUT_S,
        "success": False,
        "preflight_passed": False,
        "model_load_attempts": 0,
        "model_load_succeeded": False,
        "children": [],
        "diagnostic_report": None,
        "sources_unchanged": False,
        "error": None,
        "claims": dict(api.CLAIMS),
    }
    command = [
        nav_binding["path"],
        "-I",
        "-B",
        "-S",
        "-c",
        BOOTSTRAP,
        str(ROOT / ENTRY),
        "--plan",
        binding["path"],
        "--plan-sha256",
        binding["sha256"],
    ]
    try:
        row = run_child(command + ["--preflight-only"], output, "preflight", started + TIMEOUT_S)
        result["children"].append(row)
        result["preflight_result"] = verify_preflight(api, row, plan, binding)
        result["preflight_passed"] = True
        final_sources(api, plan, binding, nav_binding, own_sources)
        if execute:
            result["model_load_attempts"] = 1
            row = run_child(command + ["--execute"], output, "load", started + TIMEOUT_S)
            result["children"].append(row)
            result["diagnostic_report"] = verify_load_report(api, row, plan, binding)
            result["model_load_succeeded"] = True
        result["source_after"] = final_sources(api, plan, binding, nav_binding, own_sources)
        result["sources_unchanged"] = True
        if time.monotonic() >= started + TIMEOUT_S:
            raise TimeoutError("Absolute supervisor deadline exhausted before admission")
        result["success"] = True
    except BaseException as error:
        result["error"] = {"type": type(error).__name__, "message": str(error)[:2048]}
    result["elapsed_s"] = time.monotonic() - started
    result["receipt"] = write_new(output / "supervisor.json", canonical(result) + b"\n")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--output-dir", required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--preflight-only", action="store_true")
    modes.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = supervise(args.plan, args.plan_sha256, args.output_dir, execute=args.execute)
        print(
            json.dumps(
                {
                    "success": result["success"],
                    "preflight_passed": result["preflight_passed"],
                    "model_load_succeeded": result["model_load_succeeded"],
                    "receipt": result["receipt"],
                }
            )
        )
        return 0 if result["success"] else 1
    except Exception as error:
        print(
            json.dumps({"success": False, "type": type(error).__name__, "error": str(error)[:2048]})
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
