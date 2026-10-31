from __future__ import annotations

from pathlib import Path
import pytest

from scripts import eval_slow_system as slow


class _Process:
    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code

    def terminate(self):
        self.code = 0

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = -9


def _runner(tmp_path: Path, *, keep=3, code=None):
    path = tmp_path / f"runner-{len(list(tmp_path.glob('*.log')))}.log"
    handle = path.open("w", encoding="utf-8")
    handle.write("server output\n")
    handle.flush()
    runner = object.__new__(slow.LlamaServerRunner)
    runner.log_dir = tmp_path
    runner.keep_logs = keep
    runner.server_log = handle
    runner.process = _Process(code)
    runner._job_handle = None
    runner._closed = False
    runner._server_failed = code not in (None, 0)
    runner.wait_port_free = False
    runner.port = 9933
    runner._close_job = lambda: None
    return runner, path


def test_normal_close_removes_runner_log_when_retention_is_zero(tmp_path):
    runner, path = _runner(tmp_path, keep=0)
    runner.close()
    assert not path.exists()


def test_crashed_server_log_is_retained_and_error_path_is_reported(tmp_path):
    runner, path = _runner(tmp_path, code=7)
    runner._server_failed = True
    runner.close()
    assert path.is_file()


def test_constructor_places_log_in_configured_directory(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(slow.LlamaServerRunner, "_start", lambda self: seen.update(log_dir=self.log_dir))
    slow.LlamaServerRunner(tmp_path / "model.gguf", executable=tmp_path / "llama-server", log_dir=tmp_path / "logs")
    assert seen["log_dir"] == (tmp_path / "logs").resolve()


def test_startup_crash_error_includes_retained_log_path(monkeypatch, tmp_path):
    class DeadProcess(_Process):
        returncode = 7

        def __init__(self):
            self.code = 7

    monkeypatch.setattr(slow, "_cleanup_orphaned_windows_server", lambda *_: None)
    monkeypatch.setattr(slow, "_attach_windows_job", lambda process: None)
    monkeypatch.setattr(slow.subprocess, "Popen", lambda *a, **kw: DeadProcess())
    with pytest.raises(RuntimeError, match=r"log: .+llama-server-.+\.log") as error:
        slow.LlamaServerRunner(
            tmp_path / "model.gguf", executable=tmp_path / "llama-server",
            log_dir=tmp_path / "logs",
        )
    assert list((tmp_path / "logs").glob("llama-server-*.log"))


def test_retention_cap_only_removes_runner_created_logs(tmp_path):
    unrelated = tmp_path / "llama-server-user.log"
    unrelated.write_text("keep", encoding="utf-8")
    runners = [_runner(tmp_path, keep=1) for _ in range(3)]
    for runner, _ in runners:
        runner.close()
    assert unrelated.is_file()
    assert [path.exists() for _, path in runners] == [False, False, True]


def test_port_wait_ignores_time_wait_after_server_close(tmp_path):
    import socket

    from scripts.eval_slow_system import LlamaServerRunner

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.listen(1)
    client = socket.create_connection(("127.0.0.1", port))
    accepted, _ = server.accept()
    accepted.close()  # server side closes first -> its end sits in TIME_WAIT
    client.close()
    server.close()

    runner = LlamaServerRunner.__new__(LlamaServerRunner)
    runner.port = port
    runner.process = None
    runner._closed = False
    runner.wait_port_free = True
    runner.close()
