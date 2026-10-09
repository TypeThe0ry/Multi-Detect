"""Read-only Windows venv-launcher identity follow-up, never rerunning a model.

The sealed recovery supervisor remains failed. This separate check admits a
captured, hash-bound launcher/worker process chain before applying its original
PID gate to the actual worker; it does not remove or relax that gate.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))
from scripts import recover_onboard_navigation_adapter as recovery  # noqa: E402

sup = recovery.sup
CONTRACT = "multidetect.onboard-navigation-recovery-process-identity-v1"
FILES = {
    "recovery_manifest",
    "preflight",
    "completion",
    "process_observation",
    "stdout",
    "stderr",
    "report",
    "loader_report",
    "warmup_report",
}
FAILURE = "Recovery loader PID differs from the actual child process"
CLAIMS = {
    **recovery.CLAIMS,
    "model_reexecuted_by_this_check": False,
    "original_recovery_supervisor_gate_passed": False,
    "pid_gate_removed": False,
}


def windows_arguments(command_line):
    """Use Windows' own argument parser, preserving quoted paths and backslashes."""
    if (
        sys.platform != "win32"
        or not isinstance(command_line, str)
        or not 1 <= len(command_line) <= 32767
    ):
        raise ValueError("Identity follow-up requires a bounded Windows command line")
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    parse = shell.CommandLineToArgvW
    parse.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    parse.restype = ctypes.POINTER(ctypes.c_wchar_p)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    count = ctypes.c_int()
    values = parse(command_line, ctypes.byref(count))
    if not values:
        raise OSError(ctypes.get_last_error(), "Windows command-line parsing failed")
    try:
        result = [values[i] for i in range(count.value)]
        if not result or any(not isinstance(value, str) or not value for value in result):
            raise ValueError("Captured command line has empty arguments")
        return result
    finally:
        kernel.LocalFree(values)


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Process capture must provide a timestamp with UTC offset")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Process timestamps require an explicit UTC offset")
    return parsed


def verify_process_chain(observation, preflight, completion, loader, warmup, stdout, admitted):
    if (
        not isinstance(observation, dict)
        or type(observation.get("tool_exit_code")) is not int
        or observation["tool_exit_code"] != 0
        or not isinstance(observation.get("tool_chunk"), str)
        or not observation["tool_chunk"]
        or not isinstance(observation.get("processes"), list)
        or not 2 <= len(observation["processes"]) <= 16
    ):
        raise ValueError("Identity needs the bound successful live CIM process capture")
    observed = timestamp(observation.get("observed_at"))
    if observed > datetime.now(UTC):
        raise ValueError("Process capture cannot be from a future observation")
    processes = {}
    for process in observation["processes"]:
        if (
            not isinstance(process, dict)
            or type(process.get("ProcessId")) is not int
            or process["ProcessId"] <= 0
            or type(process.get("ParentProcessId")) is not int
            or process["ParentProcessId"] <= 0
            or process["ProcessId"] in processes
        ):
            raise ValueError("Captured process identity is invalid or duplicated")
        if timestamp(process.get("CreationDate")) > observed:
            raise ValueError("Process capture predates process creation")
        processes[process["ProcessId"]] = process
    launcher_pid = completion.get("child_pid")
    worker_pid = loader.get("pid")
    if (
        type(launcher_pid) is not int
        or type(worker_pid) is not int
        or launcher_pid == worker_pid
        or launcher_pid not in processes
        or worker_pid not in processes
    ):
        raise ValueError("Actual launcher and worker PID must be separate captured processes")
    launcher, worker = processes[launcher_pid], processes[worker_pid]
    parent = processes.get(launcher["ParentProcessId"])
    if parent is None or timestamp(parent["CreationDate"]) > timestamp(launcher["CreationDate"]):
        raise ValueError("Launcher must belong to the captured original recovery supervisor")
    children = [p for p in processes.values() if p["ParentProcessId"] == launcher_pid]
    if (
        len(children) != 1
        or children[0] != worker
        or timestamp(worker["CreationDate"]) < timestamp(launcher["CreationDate"])
    ):
        raise ValueError("Actual worker must be the launcher's unique direct child")
    command = preflight.get("argv")
    expected_command = [
        str(Path(sys.executable).resolve(strict=True)),
        "-B",
        "-u",
        str(Path(recovery.__file__).resolve(strict=True)),
        "--worker",
        "--recovery-manifest",
        str(Path(admitted.manifest_path)),
        "--recovery-manifest-sha256",
        admitted.manifest_binding["sha256"],
        "--output-dir",
        str(admitted.output),
    ]
    # The original caller used a repository-relative manifest path; resolve only
    # that path, without normalizing or dropping any other flag/value.
    if not isinstance(command, list) or len(command) != len(expected_command):
        raise ValueError("Original preflight worker command differs")
    normalized = command.copy()
    manifest_arg = Path(command[6])
    normalized[6] = str(
        (manifest_arg if manifest_arg.is_absolute() else WORKSPACE / manifest_arg).resolve(
            strict=True
        )
    )
    if normalized != expected_command:
        raise ValueError("Original preflight entry/manifest/output command differs")
    source_records = {r["path"]: r for r in admitted.recovery.records}
    for process in (parent, launcher, worker):
        executable = Path(process.get("ExecutablePath", "")).resolve(strict=True)
        if source_records.get(str(executable)) != sup.file_record(executable):
            raise ValueError("Captured process executable is not an exact bound runtime source")
        actual = windows_arguments(process.get("CommandLine"))
        if Path(actual[0]).resolve(strict=True) != executable:
            raise ValueError("Captured command executable differs from process executable")
        if process is not parent and actual[1:] != command[1:]:
            raise ValueError("Captured actual process command differs from the native invocation")
    if Path(launcher["ExecutablePath"]).resolve(strict=True) != Path(command[0]).resolve(
        strict=True
    ):
        raise ValueError("Popen PID is not the exact invoked venv launcher")
    if Path(parent["ExecutablePath"]).resolve(strict=True) != Path(
        worker["ExecutablePath"]
    ).resolve(strict=True) or Path(worker["ExecutablePath"]).resolve(strict=True) == Path(
        launcher["ExecutablePath"]
    ).resolve(strict=True):
        raise ValueError("Captured venv launcher and base-Python supervisor/worker paths differ")
    parent_command = windows_arguments(parent["CommandLine"])
    expected_parent = [
        str(Path(parent["ExecutablePath"]).resolve(strict=True)),
        "-u",
        "-B",
        "-X",
        "utf8",
        str(Path(recovery.__file__).resolve(strict=True)),
        "--recovery-manifest",
        str(Path(admitted.manifest_path)),
        "--recovery-manifest-sha256",
        admitted.manifest_binding["sha256"],
        "--output-dir",
        str(admitted.output),
        "--supervisor-dir",
        str(admitted.original_supervisor),
    ]
    if len(parent_command) != len(expected_parent):
        raise ValueError("Captured supervisor invocation differs from this recovery attempt")
    for index in (0, 5, 7, 11, 13):
        candidate = Path(parent_command[index])
        parent_command[index] = str(
            (candidate if candidate.is_absolute() else WORKSPACE / candidate).resolve(strict=True)
        )
    if parent_command != expected_parent:
        raise ValueError("Captured parent is not this exact recovery supervisor invocation")
    if warmup.get("warmup_observation", {}).get("pid") != worker_pid:
        raise ValueError("Warmup and actual loader worker PID differ")
    if stdout.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Original stdout exceeds the frozen event cap")
    events = [
        sup.strict_json(line)
        for line in stdout.read_bytes().splitlines()
        if line.lstrip().startswith(b"{")
    ]
    imports = [
        (i, row)
        for i, row in enumerate(events)
        if row.get("event") == "constructor_importpath_installed"
    ]
    installed = [
        i
        for i, row in enumerate(events)
        if row.get("event") == "explicit_streaming_loader_installed"
    ]
    finished = [
        i for i, row in enumerate(events) if row.get("event") == "no_warmup_worker_finished"
    ]
    if (
        len(imports) != 1
        or len(installed) != 1
        or len(finished) != 1
        or not installed[0] < imports[0][0] < finished[0]
        or imports[0][1].get("pid") != worker_pid
        or any(
            imports[0][1].get(key) != value for key, value in recovery.runtime.DECLARATIONS.items()
        )
    ):
        raise ValueError("Original constructor import event is not scoped to the actual worker PID")
    return {
        "launcher_pid": launcher_pid,
        "actual_worker_pid": worker_pid,
        "actual_supervisor_pid": parent["ProcessId"],
        "unique_direct_child_verified": True,
        "launcher_parent_supervisor_invocation_verified": True,
        "captured_live_process_creation_precedes_observation": True,
        "creation_and_observation_timestamps": {
            "launcher_created": launcher["CreationDate"],
            "worker_created": worker["CreationDate"],
            "observed_at": observation["observed_at"],
        },
        "runtime_executables_reverified": True,
        "captured_worker_arguments_exact": True,
        "constructor_import_loader_and_warmup_worker_pid_equal": True,
        "process_exit_time_independently_measured": False,
    }


def read_manifest(args):
    path = Path(args.identity_manifest).resolve(strict=True)
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Identity manifest exceeds bounded input cap")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != args.identity_manifest_sha256:
        raise ValueError("Identity manifest SHA differs")
    manifest = sup.strict_json(raw)
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
        or manifest.get("contract") != CONTRACT
        or manifest.get("entrypoint_sha256") != sup.sha256_file(__file__)
        or not isinstance(manifest.get("files"), dict)
        or set(manifest["files"]) != FILES
        or not isinstance(manifest.get("source_files"), list)
        or len(manifest["source_files"]) != 2
    ):
        raise ValueError("Identity schema/entrypoint/bindings differ")
    records = {}
    for record in [*manifest["source_files"], *manifest["files"].values()]:
        recovery.verify_record(record)
        if record["path"] in records:
            raise ValueError("Identity bindings repeat a path")
        records[record["path"]] = record
    for source in (
        Path(__file__),
        WORKSPACE / "tests/test_verify_onboard_navigation_recovery_identity.py",
    ):
        if records.get(str(source.resolve(strict=True))) != sup.file_record(source):
            raise ValueError("Identity must explicitly bind its new entry and CPU tests")
    files = manifest["files"]
    documents = {
        key: recovery.bound_json(record)
        for key, record in files.items()
        if key not in {"recovery_manifest", "stdout", "stderr"}
    }
    old_args = SimpleNamespace(
        recovery_manifest=Path(files["recovery_manifest"]["path"]),
        recovery_manifest_sha256=files["recovery_manifest"]["sha256"],
    )
    old = recovery.read_manifest(old_args)
    output = Path(files["report"]["path"]).parent
    original_supervisor = Path(files["completion"]["path"]).parent
    if any(
        Path(files[key]["path"]) != directory / name
        for key, directory, name in (
            ("report", output, "report.json"),
            ("loader_report", output, "streaming-loader-report.json"),
            ("warmup_report", output, "allocator-warmup-report.json"),
            ("completion", original_supervisor, "completion.json"),
            ("preflight", original_supervisor, "preflight.json"),
            ("process_observation", original_supervisor, "process-observation.json"),
            ("stdout", original_supervisor, "stdout.bin"),
            ("stderr", original_supervisor, "stderr.bin"),
        )
    ):
        raise ValueError("Identity must bind one exact recovery attempt's output/supervisor files")
    completion, preflight = documents["completion"], documents["preflight"]
    if (
        completion.get("contract") != recovery.CONTRACT
        or preflight.get("contract") != recovery.CONTRACT
        or type(completion.get("supervisor_exit_code")) is not int
        or completion["supervisor_exit_code"] != 1
        or type(completion.get("child_returncode_raw")) is not int
        or completion["child_returncode_raw"] != 0
        or completion.get("failure_type") != "ValueError"
        or completion.get("failure") != FAILURE
        or completion.get("worker_result_verified") is not False
        or completion.get("sources_unchanged") is not True
        or completion.get("sources_before") != old.records
        or completion.get("sources_after") != old.records
        or preflight.get("sources_before") != old.records
        or any(
            completion.get(key) is not value or preflight.get(key) is not value
            for key, value in recovery.CLAIMS.items()
        )
    ):
        raise ValueError(
            "Only a source-unchanged native-zero worker rejected solely by launcher PID is admitted"
        )
    for record in old.records:
        if record["path"] in records and records[record["path"]] != record:
            raise ValueError("Identity contradicts its original frozen recovery binding")
        records[record["path"]] = record
    records[str(path)] = sup.file_record(path)
    return SimpleNamespace(
        manifest=manifest,
        recovery=old,
        old_args=old_args,
        documents=documents,
        manifest_path=old_args.recovery_manifest,
        manifest_binding=files["recovery_manifest"],
        output=output,
        original_supervisor=original_supervisor,
        records=[records[key] for key in sorted(records)],
    )


def run(args):
    admitted = read_manifest(args)
    output = sup.resolve_fresh(args.output_dir, admitted.recovery.root)
    sup.protect_sealed_directories(output, admitted.recovery.plan, admitted.recovery.root)
    if any(output.is_relative_to(Path(r["path"]).parent) for r in admitted.records):
        raise ValueError(
            "Identity output cannot pollute a bound source or prior evidence directory"
        )
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": 1,
        "contract": CONTRACT,
        **CLAIMS,
        "native_exit_code": 1,
        "identity_manifest_sha256": args.identity_manifest_sha256,
        "original_training_native_exit_code": 1,
        "original_recovery_supervisor_exit_code": 1,
        "original_worker_native_exit_code": 0,
        "sources_before": admitted.records,
        "sources_unchanged": False,
        "fresh_saved_adapter_reload_verified": False,
    }
    try:
        docs, files = admitted.documents, admitted.manifest["files"]
        identity = verify_process_chain(
            docs["process_observation"],
            docs["preflight"],
            docs["completion"],
            docs["loader_report"],
            docs["warmup_report"],
            Path(files["stdout"]["path"]),
            admitted,
        )
        result = recovery.verify_success(
            admitted.output,
            Path(files["stdout"]["path"]),
            admitted.old_args,
            admitted.recovery,
            expected_pid=identity["actual_worker_pid"],
        )
        after = read_manifest(args)
        report["sources_after"] = after.records
        if after.records != admitted.records:
            raise ValueError("Original input or native evidence changed during identity follow-up")
        report.update(
            native_exit_code=0,
            sources_unchanged=True,
            fresh_saved_adapter_reload_verified=True,
            original_pid_failure_preserved=True,
            process_identity=identity,
            original_worker_result=result,
        )
    except Exception as exc:
        traceback.print_exc()
        report.update(failure_type=type(exc).__name__, failure=str(exc))
    finally:
        sup.create_json(output / "report.json", report)
        sup.emit("navigation_recovery_identity_followup_finished", **report)
    return report["native_exit_code"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity-manifest", type=Path, required=True)
    parser.add_argument("--identity-manifest-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        return run(args)
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
