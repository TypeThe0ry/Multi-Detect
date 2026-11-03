"""Pure CPU follow-up of the captured Windows launcher/worker identity seam."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import verify_onboard_navigation_recovery_identity as entry

sup = entry.sup
spec = importlib.util.spec_from_file_location(
    "identity_recovery_cpu_fixture",
    Path(__file__).with_name("test_recover_onboard_navigation_adapter.py"),
)
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def invocation(tmp_path, monkeypatch):
    old = base.invocation.__wrapped__(tmp_path, monkeypatch)
    old_manifest = json.loads(old.recovery_manifest.read_bytes())
    # Keep the executable identity tied to this interpreter. The captured
    # command line is Windows-formatted below, but the fixture must also run
    # on POSIX where the Windows venv path does not exist.
    base_python = Path(sys.executable).resolve()
    old_manifest["source_files"].append(sup.file_record(base_python))
    write(old.recovery_manifest, old_manifest)
    old.recovery_manifest_sha256 = sup.sha256_file(old.recovery_manifest)
    popen = base.fake_child(old, monkeypatch)
    worker_pid, launcher_pid = os.getpid(), os.getpid() + 1

    def launcher(*args, **kwargs):
        result = popen(*args, **kwargs)
        return SimpleNamespace(pid=launcher_pid, wait=result.wait)

    assert entry.recovery.supervise(old, popen_factory=launcher) == 1
    preflight = json.loads((old.supervisor_dir / "preflight.json").read_bytes())
    command = preflight["argv"]
    parent_command = [
        str(base_python),
        "-u",
        "-B",
        "-X",
        "utf8",
        str(Path(entry.recovery.__file__).resolve()),
        "--recovery-manifest",
        str(old.recovery_manifest),
        "--recovery-manifest-sha256",
        old.recovery_manifest_sha256,
        "--output-dir",
        str(old.output_dir),
        "--supervisor-dir",
        str(old.supervisor_dir),
    ]
    observation = {
        "observed_at": "2026-10-07T13:50:03+00:00",
        "tool_chunk": "CPU fixture, not actual CIM evidence",
        "tool_exit_code": 0,
        "processes": [
            {
                "ProcessId": launcher_pid,
                "ParentProcessId": worker_pid + 2,
                "ExecutablePath": command[0],
                "CommandLine": subprocess.list2cmdline(command),
                "CreationDate": "2026-10-07T13:49:53.463+00:00",
            },
            {
                "ProcessId": worker_pid,
                "ParentProcessId": launcher_pid,
                "ExecutablePath": str(base_python),
                "CommandLine": subprocess.list2cmdline([str(base_python), *command[1:]]),
                "CreationDate": "2026-10-07T13:49:53.507+00:00",
            },
            {
                "ProcessId": worker_pid + 2,
                "ParentProcessId": worker_pid + 3,
                "ExecutablePath": str(base_python),
                "CommandLine": subprocess.list2cmdline(parent_command),
                "CreationDate": "2026-10-07T13:49:41.885+00:00",
            },
        ],
    }
    write(old.supervisor_dir / "process-observation.json", observation)
    files = {
        "recovery_manifest": sup.file_record(old.recovery_manifest),
        **{
            key: sup.file_record(old.supervisor_dir / name)
            for key, name in (
                ("preflight", "preflight.json"),
                ("completion", "completion.json"),
                ("process_observation", "process-observation.json"),
                ("stdout", "stdout.bin"),
                ("stderr", "stderr.bin"),
            )
        },
        **{
            key: sup.file_record(old.output_dir / name)
            for key, name in (
                ("report", "report.json"),
                ("loader_report", "streaming-loader-report.json"),
                ("warmup_report", "allocator-warmup-report.json"),
            )
        },
    }
    config_dir = tmp_path / "identity-config"
    config_dir.mkdir()
    manifest = config_dir / "identity.json"
    write(
        manifest,
        {
            "schema_version": 1,
            "contract": entry.CONTRACT,
            "entrypoint_sha256": sup.sha256_file(entry.__file__),
            "files": files,
            "source_files": [sup.file_record(entry.__file__), sup.file_record(__file__)],
        },
    )
    return SimpleNamespace(
        identity_manifest=manifest,
        identity_manifest_sha256=sup.sha256_file(manifest),
        output_dir=tmp_path / "identity-output",
        old=old,
        worker_pid=worker_pid,
        launcher_pid=launcher_pid,
    )


def update_binding(args, path):
    manifest = json.loads(args.identity_manifest.read_bytes())
    current = sup.file_record(path)
    for key, record in manifest["files"].items():
        if record["path"] == current["path"]:
            manifest["files"][key] = current
    write(args.identity_manifest, manifest)
    args.identity_manifest_sha256 = sup.sha256_file(args.identity_manifest)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows CommandLineToArgvW")
def test_windows_native_argument_parser_preserves_quoted_paths():
    values = [r"D:\a path\python.exe", "-B", r"D:\source path\entry.py", "--worker", "bound sha"]
    assert entry.windows_arguments(subprocess.list2cmdline(values)) == values


def test_readonly_exact_chain_followup_closes_reload_not_original_failure(invocation):
    before = set(sys.modules)
    original_completion = invocation.old.supervisor_dir / "completion.json"
    original_report = invocation.old.old.output_dir / "report.json"
    identities = (sup.sha256_file(original_completion), sup.sha256_file(original_report))
    assert entry.run(invocation) == 0
    report = json.loads((invocation.output_dir / "report.json").read_bytes())
    assert report["original_training_native_exit_code"] == 1
    assert report["original_recovery_supervisor_exit_code"] == 1
    assert report["original_worker_native_exit_code"] == 0
    assert report["fresh_saved_adapter_reload_verified"] is True
    assert report["process_identity"]["actual_worker_pid"] == invocation.worker_pid
    assert report["process_identity"]["launcher_pid"] == invocation.launcher_pid
    assert report["sources_before"] == report["sources_after"]
    assert all(report[key] is False for key in entry.CLAIMS)
    assert identities == (sup.sha256_file(original_completion), sup.sha256_file(original_report))
    assert not {"torch", "transformers", "peft", "accelerate"} & (sys.modules.keys() - before)


@pytest.mark.parametrize(
    "kind",
    ["native", "original-supervisor", "reason", "sources", "promotion", "entry", "missing-source"],
)
def test_identity_admission_rejects_non_pid_failures_before_outputs(invocation, kind):
    manifest = json.loads(invocation.identity_manifest.read_bytes())
    if kind == "entry":
        manifest["entrypoint_sha256"] = "0" * 64
    elif kind == "missing-source":
        manifest["source_files"].pop()
    else:
        path = Path(manifest["files"]["completion"]["path"])
        result = json.loads(path.read_bytes())
        if kind == "native":
            result["child_returncode_raw"] = 1
        elif kind == "original-supervisor":
            result["supervisor_exit_code"] = 0
        elif kind == "reason":
            result["failure"] = "different model or data failure"
        elif kind == "sources":
            result["sources_after"] = []
        else:
            result["candidate_promoted"] = True
        write(path, result)
        update_binding(invocation, path)
        manifest = json.loads(invocation.identity_manifest.read_bytes())
    write(invocation.identity_manifest, manifest)
    invocation.identity_manifest_sha256 = sup.sha256_file(invocation.identity_manifest)
    with pytest.raises(ValueError):
        entry.run(invocation)
    assert not invocation.output_dir.exists()


@pytest.mark.parametrize(
    "kind",
    [
        "parent",
        "duplicate",
        "siblings",
        "worker-pid",
        "tool-exit",
        "time",
        "future",
        "arguments",
        "executable",
        "warmup-pid",
        "constructor-pid",
        "worker-result",
        "parent-missing",
        "parent-arguments",
        "parent-time",
        "same-executable",
    ],
)
def test_actual_identity_or_output_drift_is_never_accepted(invocation, kind):
    manifest = json.loads(invocation.identity_manifest.read_bytes())
    if kind in {"warmup-pid", "worker-result"}:
        key = "warmup_report" if kind == "warmup-pid" else "report"
        path = Path(manifest["files"][key]["path"])
        result = json.loads(path.read_bytes())
        if kind == "warmup-pid":
            result["warmup_observation"]["pid"] += 7
        else:
            result["reload_check"]["loaded_adapter_parameter_sha256"] = "d" * 64
        write(path, result)
    elif kind == "constructor-pid":
        path = Path(manifest["files"]["stdout"]["path"])
        rows = [json.loads(line) for line in path.read_bytes().splitlines()]
        for row in rows:
            if row.get("event") == "constructor_importpath_installed":
                row["pid"] += 7
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    else:
        path = Path(manifest["files"]["process_observation"]["path"])
        result = json.loads(path.read_bytes())
        worker = result["processes"][1]
        if kind == "parent-missing":
            result["processes"].pop()
        elif kind == "parent-arguments":
            result["processes"][2]["CommandLine"] = result["processes"][2]["CommandLine"].replace(
                "--supervisor-dir", "--train"
            )
        elif kind == "parent-time":
            result["processes"][2]["CreationDate"] = "2026-10-07T13:49:54+00:00"
        elif kind == "same-executable":
            worker["ExecutablePath"] = result["processes"][0]["ExecutablePath"]
            worker["CommandLine"] = result["processes"][0]["CommandLine"]
        elif kind == "parent":
            worker["ParentProcessId"] += 7
        elif kind == "duplicate":
            result["processes"].append(worker.copy())
        elif kind == "siblings":
            result["processes"].append({**worker, "ProcessId": worker["ProcessId"] + 7})
        elif kind == "worker-pid":
            worker["ProcessId"] += 7
        elif kind == "tool-exit":
            result["tool_exit_code"] = 1
        elif kind == "time":
            worker["CreationDate"] = "2026-10-07T13:51:00+00:00"
        elif kind == "future":
            result["observed_at"] = "2099-10-07T13:50:03+00:00"
        elif kind == "arguments":
            worker["CommandLine"] = worker["CommandLine"].replace("--worker", "--train")
        else:
            worker["ExecutablePath"] = str(Path(entry.__file__).resolve())
        write(path, result)
    update_binding(invocation, path)
    assert entry.run(invocation) == 1
    report = json.loads((invocation.output_dir / "report.json").read_bytes())
    assert report["fresh_saved_adapter_reload_verified"] is False


@pytest.mark.parametrize("which", ["existing", "original-output", "source"])
def test_followup_cannot_overwrite_or_pollute_bound_sources(invocation, which):
    if which == "existing":
        invocation.output_dir.mkdir()
        (invocation.output_dir / "user-marker").write_bytes(b"user content")
    elif which == "original-output":
        invocation.output_dir = invocation.old.output_dir / "new-output"
    else:
        invocation.output_dir = invocation.identity_manifest.parent / "new-output"
    before = set(invocation.identity_manifest.parent.parent.rglob("*"))
    with pytest.raises((ValueError, FileExistsError)):
        entry.run(invocation)
    assert set(invocation.identity_manifest.parent.parent.rglob("*")) == before
