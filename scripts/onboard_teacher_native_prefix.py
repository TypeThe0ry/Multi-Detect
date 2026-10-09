"""Bounded original native prefixes, not a silently truncated complete log.

Two owned pipe readers continuously drain a single child. Prefix memory is
bounded independently of total output. Complete-output SHA is present only
when both streams reached EOF within the deadline/output bound. No command
allowlist or container permission is supplied by this byte-capture helper.
"""

from __future__ import annotations

import hashlib
import subprocess
import threading
import time


def capture_native_prefix(
    command, *, timeout_s, prefix_bytes=128 * 1024, output_limit_bytes=16 * 1024 * 1024
):
    if not isinstance(command, list) or not command or any(type(s) is not str for s in command):
        raise ValueError("Explicit native argument vector required")
    if type(timeout_s) not in (int, float) or not 0 < timeout_s <= 60:
        raise ValueError("Bounded native deadline required")
    if type(prefix_bytes) is not int or not 1 <= prefix_bytes <= 256 * 1024:
        raise ValueError("Bounded original prefix required")
    if (
        type(output_limit_bytes) is not int
        or not prefix_bytes <= output_limit_bytes <= 32 * 1024 * 1024
    ):
        raise ValueError("Explicit bounded stream output required")
    child = None
    lock, stop = threading.Lock(), threading.Event()
    total = 0
    streams = {}

    def read(name, pipe):
        nonlocal total
        prefix, digest, count, error, eof = bytearray(), hashlib.sha256(), 0, None, False
        try:
            while not stop.is_set():
                chunk = pipe.read(4096)
                if not chunk:
                    eof = True
                    break
                with lock:
                    total += len(chunk)
                    exceeded = total > output_limit_bytes
                count += len(chunk)
                digest.update(chunk)
                prefix.extend(chunk[: max(0, prefix_bytes - len(prefix))])
                if exceeded:
                    error = "combined native output bound exceeded"
                    stop.set()
                    break
        except BaseException as failure:
            error = f"{type(failure).__name__}: {failure}"[:1024]
            stop.set()
        finally:
            streams[name] = {
                "prefix": bytes(prefix),
                "observed_bytes": count,
                "EOF": eof,
                "sha256_observed_bytes": digest.hexdigest(),
                "error": error,
            }

    threads = []
    started_threads = []
    try:
        child = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL
        )
        for name, pipe in (("stdout", child.stdout), ("stderr", child.stderr)):
            thread = threading.Thread(
                target=read, args=(name, pipe), daemon=True, name="owned-native-" + name
            )
            threads.append(thread)
            thread.start()
            started_threads.append(thread)
        deadline, terminated, timed_out = time.monotonic() + timeout_s, False, False
        while child.poll() is None:
            if stop.is_set() or time.monotonic() >= deadline:
                timed_out = not stop.is_set()
                child.kill()  # only this freshly spawned, owned native child
                terminated = True
                break
            time.sleep(0.01)
        exit_code = child.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=2)
        live = any(thread.is_alive() for thread in threads)
        complete = (
            not live
            and not timed_out
            and not terminated
            and all(
                streams[name]["EOF"] and streams[name]["error"] is None
                for name in ("stdout", "stderr")
            )
        )
        result = {
            "native_exit_code": exit_code,
            "owned_process_id": child.pid,
            "owned_process_exited": child.poll() is not None,
            "reader_threads_exited": not live,
            "timeout": timed_out,
            "owned_process_killed": terminated,
            "output_limit_bytes": output_limit_bytes,
            "prefix_limit_bytes_per_stream": prefix_bytes,
            "complete_output_observed": complete,
            "streams": streams,
        }
        for item in streams.values():
            item["all_bytes_retained"] = complete and item["observed_bytes"] == len(item["prefix"])
            item["full_output_sha256"] = item["sha256_observed_bytes"] if complete else None
            item["retained_prefix_bytes"] = len(item["prefix"])
        return result
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=2)
        for thread in started_threads:
            if thread.is_alive():
                thread.join(timeout=2)
        # Do not close a pipe under a still-running reader. The supervisor owns
        # final process-tree teardown; this condition is never a successful read.
        if child is not None:
            for index, pipe in enumerate((child.stdout, child.stderr)):
                if index >= len(threads) or not threads[index].is_alive():
                    pipe.close()
