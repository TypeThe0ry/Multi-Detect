from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import procfs_futex_owner as probe


@pytest.mark.parametrize(
    "text",
    [
        "running",
        "-1 0x1 0x2",
        "449 0x1000 128 2 0 0 0 0 0",
        "202 0x1000 129 2 0 0 0 0 0",
        "202 0x1000 128 1 0 0 0 0 0",
        "202 0x1004 128 2 0 0 0 0 0",
        "202 0xffff800000001000 128 2 0 0 0 0 0",
    ],
)
def test_rejects_nonwait_unknown_abi_and_misaligned_address(text):
    assert probe.parse_futex_wait(text) is None


def test_exact_wait_and_bitset_are_only_candidates():
    for operation in (128, 137):
        assert probe.parse_futex_wait(f"202 0x1000 {operation} 2 0 0 0 0 0") == {
            "syscall_number": 202,
            "address": 4096,
            "operation": operation,
            "expected": 2,
        }


def test_unknown_pid_namespace_and_limits_never_open_memory():
    with patch.object(probe.os, "open", side_effect=AssertionError("must not read a PID")):
        result = probe.observe_known_tasks(
            {"process_threads": {"collection_path": "container"}}, []
        )
        assert result["errors"] and result["counters"]["process_memory_bytes"] == 0
        sample = {
            "process_threads": {
                "collection_path": "host_proc",
                "threads": {
                    "gz": [{"pid": 1, "tid": 1}] * (probe.MAX_TASK_ROWS + 1),
                },
            }
        }
        assert probe.observe_known_tasks(sample, [])["errors"]


def test_missing_starttime_and_unrecognized_candidate_do_not_open_pid():
    sample = {
        "process_threads": {
            "collection_path": "host_proc",
            "threads": {
                "gz": [{"pid": 123, "tid": 123}],
            },
        }
    }
    with patch.object(probe.os, "open", side_effect=AssertionError("identity not bound")):
        result = probe.observe_known_tasks(sample, [], candidate_pid=123)
        bad = probe.observe_known_tasks(sample, [], candidate_pid=456)
    assert "starttime" in result["processes"][0]["errors"][0]
    assert result["counters"]["process_memory_reads"] == 0
    assert "explicitly identified" in bad["errors"][0]


@pytest.fixture(scope="module")
def pthread_helper(tmp_path_factory):
    if probe.platform.system() != "Linux" or probe.platform.machine() != "x86_64":
        pytest.skip("Linux x86_64 only")
    compiler = shutil.which("cc")
    if not compiler:
        pytest.skip("no C compiler; do not install")
    folder = tmp_path_factory.mktemp("owned-pthread")
    source = Path(__file__).parent / "fixtures/procfs_mutex_child.c"
    binary = folder / "mutex-child"
    subprocess.run(
        [compiler, "-std=c11", "-pthread", str(source), "-ldl", "-o", str(binary)],
        check=True,
        timeout=10,
    )
    return binary


def test_real_owned_child_roundtrip_rejects_stale_layout_and_denied_memory(pthread_helper):
    child = subprocess.Popen(
        [str(pthread_helper)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "CUDA_VISIBLE_DEVICES": ""},
    )
    try:
        measured = json.loads(child.stdout.readline())
        assert measured["pid"] == child.pid
        pid, owner = child.pid, measured["owner_tid"]
        identity = probe._identity(Path("/proc") / str(pid))
        proof = {
            **measured,
            "architecture": "x86_64",
            "endian": "little",
            "libc_sha256": hashlib.sha256(Path(measured["libc_path"]).read_bytes()).hexdigest(),
            "verification": "owned-pthread-child-raw-owner-roundtrip",
            "proof_id": "test-only",
        }
        rows = [{"pid": pid, "tid": tid, "starttime_ticks": identity[1]} for tid in (pid, owner)]
        sample = {"process_threads": {"collection_path": "host_proc", "threads": {"gz": rows}}}
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if probe.parse_futex_wait(probe._text(Path(f"/proc/{pid}/task/{pid}/syscall"), 256)):
                break
            time.sleep(0.005)
        observed = probe.observe_known_tasks(sample, [proof], candidate_pid=pid)
        candidate = observed["processes"][0]["mutex_owner_candidate"]
        assert candidate["status"] == "stable_recursive_mutex_candidate"
        assert int(candidate["candidate_futex_address"], 16) == int(measured["mutex_address"], 16)
        assert candidate["owner_host_lwp_candidate"] == owner
        assert candidate["fields"][0] == candidate["fields"][1]
        assert candidate["fields"][0]["count"] == 2
        assert (
            not candidate["node_shared_mutex_verified"] and not candidate["deadlock_cycle_proven"]
        )
        assert observed["counters"] == {"process_memory_reads": 2, "process_memory_bytes": 40}
        assert observed["limits"]["total_process_memory_bytes_max"] == 40
        unsupported = probe.observe_known_tasks(
            sample, [{**proof, "libc_sha256": "0" * 64}], candidate_pid=pid
        )
        assert unsupported["counters"]["process_memory_bytes"] == 0
        assert "layout proof" in unsupported["processes"][0]["mutex_owner_candidate"]["error"]
        stale = {
            "process_threads": {
                "collection_path": "host_proc",
                "threads": {
                    "gz": [{**row, "starttime_ticks": identity[1] + 1} for row in rows],
                },
            }
        }
        refused = probe.observe_known_tasks(stale, [proof], candidate_pid=pid)
        assert refused["counters"]["process_memory_reads"] == 0
        assert "PID reuse" in refused["processes"][0]["errors"][0]
        original_open = probe.os.open

        def denied(path, *args, **kwargs):
            if Path(path).name == "mem":
                raise PermissionError("test-only proc memory access denied")
            return original_open(path, *args, **kwargs)

        with patch.object(probe.os, "open", side_effect=denied):
            refused = probe.observe_known_tasks(sample, [proof], candidate_pid=pid)
        assert refused["counters"]["process_memory_bytes"] == 0
        assert "PermissionError" in refused["processes"][0]["mutex_owner_candidate"]["error"]
        metadata_only = probe.observe_known_tasks(sample, [proof])
        assert metadata_only["counters"]["process_memory_reads"] == 0
        assert metadata_only["memory_candidate_status"] == "not_requested"
        output, error = child.communicate(timeout=8)
        assert child.returncode == 0 and "RELEASED_NORMALLY" in output, error
    finally:
        if child.poll() is None:
            child.terminate()  # Only this test's explicitly created child.
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=1)
