"""Tiny local owned native children, no Docker/runtime acceptance."""

import hashlib
import subprocess
import sys
import threading

import pytest

from scripts.onboard_teacher_native_prefix import capture_native_prefix


def run(code, **options):
    return capture_native_prefix([sys.executable, "-I", "-B", "-c", code], timeout_s=3, **options)


def test_original_prefix_hash_and_native_status_without_memory_growth():
    result = run(
        "import sys; sys.stdout.buffer.write(b'a'*500000); "
        "sys.stderr.buffer.write(b'b'*300000); sys.exit(7)",
        prefix_bytes=1024,
    )
    assert result["native_exit_code"] == 7
    assert result["complete_output_observed"]
    assert result["owned_process_exited"] and result["reader_threads_exited"]
    for name, byte, count in (("stdout", b"a", 500000), ("stderr", b"b", 300000)):
        stream = result["streams"][name]
        assert stream["prefix"] == byte * 1024
        assert stream["observed_bytes"] == count
        assert stream["full_output_sha256"] == hashlib.sha256(byte * count).hexdigest()
        assert not stream["all_bytes_retained"]


def test_small_output_is_fully_retained():
    result = run("print('hello')")
    assert result["native_exit_code"] == 0 and result["complete_output_observed"]
    assert result["streams"]["stdout"]["prefix"].strip() == b"hello"
    assert all(item["all_bytes_retained"] for item in result["streams"].values())


def test_output_limit_retains_partial_original_and_fails_complete_claim():
    result = run(
        "import sys; sys.stdout.buffer.write(b'x'*1000000)",
        prefix_bytes=128,
        output_limit_bytes=4096,
    )
    assert not result["complete_output_observed"]
    assert result["streams"]["stdout"]["prefix"] == b"x" * 128
    assert result["streams"]["stdout"]["full_output_sha256"] is None


def test_timeout_reaps_owned_child_and_preserves_partial_bytes():
    result = capture_native_prefix(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import time; print('partial',flush=True); time.sleep(3)",
        ],
        timeout_s=0.5,
    )
    assert result["timeout"] and result["owned_process_killed"]
    assert result["owned_process_exited"] and result["reader_threads_exited"]
    assert b"partial" in result["streams"]["stdout"]["prefix"]
    assert not result["complete_output_observed"]


@pytest.mark.parametrize(
    "options", [{"prefix_bytes": True}, {"output_limit_bytes": True}, {"prefix_bytes": 0}]
)
def test_invalid_limits_do_not_start_child(options):
    with pytest.raises(ValueError):
        run("raise RuntimeError('not run')", **options)


def test_second_reader_start_failure_still_reaps_child(monkeypatch):
    children = []
    original_popen = subprocess.Popen
    original_start = threading.Thread.start
    starts = []

    def popen(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child

    def start(thread):
        starts.append(thread)
        if len(starts) == 2:
            raise RuntimeError("fixture second reader start failed")
        return original_start(thread)

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(threading.Thread, "start", start)
    with pytest.raises(RuntimeError, match="second reader"):
        run("import time; time.sleep(10)")
    assert len(children) == 1 and children[0].poll() is not None
    assert not starts[0].is_alive()
    assert children[0].stdout.closed and children[0].stderr.closed


def test_second_reader_constructor_failure_still_reaps_child(monkeypatch):
    children, threads = [], []
    original_popen, original_thread = subprocess.Popen, threading.Thread

    def popen(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child

    def thread(*args, **kwargs):
        if threads:
            raise MemoryError("fixture second thread construction failed")
        result = original_thread(*args, **kwargs)
        threads.append(result)
        return result

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(threading, "Thread", thread)
    with pytest.raises(MemoryError, match="second thread construction"):
        run("import time; time.sleep(10)")
    assert len(children) == 1 and children[0].poll() is not None
    assert not threads[0].is_alive()
    assert children[0].stdout.closed and children[0].stderr.closed
