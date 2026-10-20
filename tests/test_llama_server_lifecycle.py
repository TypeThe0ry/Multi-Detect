from pathlib import Path

import pytest

from scripts import eval_slow_system as slow


def _owner(pid=123, parent=999, name="llama-server.exe", command=""):
    return {"ProcessId": pid, "ParentProcessId": parent, "Name": name, "CommandLine": command}


def test_windows_orphan_artifact_server_is_terminated(monkeypatch, tmp_path):
    monkeypatch.setattr(slow.sys, "platform", "win32")
    model = tmp_path / "artifacts" / "models" / "identity.gguf"
    cmd = f'llama-server.exe -m "{model}" --port 9941'
    monkeypatch.setattr(slow, "_windows_port_processes", lambda port: [_owner(command=cmd)])
    monkeypatch.setattr(slow, "_windows_process_exists", lambda pid: False)
    killed = []
    monkeypatch.setattr(slow, "_terminate_windows_process", killed.append)

    slow._cleanup_orphaned_windows_server(9941, model, tmp_path)

    assert killed == [123]


def test_windows_live_owner_is_reported_without_termination(monkeypatch, tmp_path):
    monkeypatch.setattr(slow.sys, "platform", "win32")
    model = tmp_path / "artifacts" / "models" / "identity.gguf"
    cmd = f'llama-server.exe -m "{model}" --port 9941'
    monkeypatch.setattr(slow, "_windows_port_processes", lambda port: [_owner(command=cmd)])
    monkeypatch.setattr(slow, "_windows_process_exists", lambda pid: True)
    killed = []
    monkeypatch.setattr(slow, "_terminate_windows_process", killed.append)

    with pytest.raises(RuntimeError, match=r"PID 123.*llama-server.exe"):
        slow._cleanup_orphaned_windows_server(9941, model, tmp_path)
    assert killed == []


def test_windows_unrelated_owner_is_not_terminated(monkeypatch, tmp_path):
    monkeypatch.setattr(slow.sys, "platform", "win32")
    model = tmp_path / "artifacts" / "models" / "identity.gguf"
    monkeypatch.setattr(
        slow,
        "_windows_port_processes",
        lambda port: [_owner(name="python.exe", command="python unrelated.py")],
    )
    killed = []
    monkeypatch.setattr(slow, "_terminate_windows_process", killed.append)

    with pytest.raises(RuntimeError, match=r"PID 123.*python unrelated.py"):
        slow._cleanup_orphaned_windows_server(9941, model, tmp_path)
    assert killed == []


def test_non_windows_cleanup_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(slow.sys, "platform", "linux")
    monkeypatch.setattr(
        slow, "_windows_port_processes", lambda port: (_ for _ in ()).throw(AssertionError)
    )
    slow._cleanup_orphaned_windows_server(9941, Path("model.gguf"), tmp_path)


def test_windows_spawn_job_object_uses_kill_on_close(monkeypatch):
    calls = []

    class Kernel:
        def CreateJobObjectW(self, *_args):
            calls.append("create")
            return 77

        def SetInformationJobObject(self, job, info_class, info, size):
            calls.append(("set", job, info_class, size))
            return 1

        def AssignProcessToJobObject(self, job, process):
            calls.append(("assign", job, process.value))
            return 1

    monkeypatch.setattr(slow.ctypes, "WinDLL", lambda *args, **kwargs: Kernel())
    process = type("Process", (), {"_handle": 42})()

    assert slow._attach_windows_job(process) == 77
    assert calls[0] == "create"
    assert calls[1][0:3] == ("set", 77, 9)
    assert calls[2] == ("assign", 77, 42)


def test_port_owner_query_builds_its_powershell_script(monkeypatch) -> None:
    # The real query (not a fake) must build: a str.format() over the script's
    # own braces raised KeyError and broke every onboard policy start-up in SITL.
    calls = []

    class Result:
        returncode = 0
        stdout = '{"ProcessId": 7, "ParentProcessId": 1, "Name": "x.exe", "CommandLine": "x"}'
        stderr = ""

    monkeypatch.setattr(slow.sys, "platform", "win32")
    monkeypatch.setattr(
        slow.subprocess, "run", lambda argv, **kwargs: calls.append(argv) or Result()
    )
    owners = slow._windows_port_processes(9941)
    assert owners == [{"ProcessId": 7, "ParentProcessId": 1, "Name": "x.exe", "CommandLine": "x"}]
    script = calls[0][-1]
    assert "-LocalPort 9941 " in script and "{ $owners -contains $_.ProcessId }" in script
