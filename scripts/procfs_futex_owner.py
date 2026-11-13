"""Bounded, read-only futex/mutex owner candidates for already identified tasks.

No process discovery, arbitrary address API, ptrace, signals, code injection, or
inferior calls. This is an optional failure-stage observation, not a diagnosis.
The caller must supply host-proc task rows with starttime identity and a layout
proof measured using a pthread child linked to the exact target libc SHA.
"""

from __future__ import annotations

import hashlib
import os
import platform
import struct
import time
from pathlib import Path
from typing import Any

MAX_TASK_ROWS = 512
MAX_PROCESSES = 4
MAX_GZ_MAINS = 1
MEMORY_READ_BYTES = 20
MEMORY_READS_PER_MAIN = 2
MAX_ELAPSED_S = 0.25  # Checked between bounded reads; not a kernel I/O timeout.
OFFSETS = {"lock": 0, "count": 4, "owner": 8, "nusers": 12, "kind": 16}


def _text(path: Path, limit: int) -> str:
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError(f"bounded procfs text exceeded {limit} bytes: {path.name}")
    return value.decode("utf-8", errors="replace").strip()


def _identity(process: Path) -> tuple[int, int]:
    line = _text(process / "stat", 2048)
    close = line.rfind(")")
    fields = line[close + 2 :].split()
    if close < 0 or len(fields) < 20:
        raise ValueError("invalid process stat")
    return int(line.split("(", 1)[0]), int(fields[19])


def _status(path: Path, pid: int, tid: int) -> dict[str, Any]:
    fields = dict(line.split(":", 1) for line in _text(path, 4096).splitlines() if ":" in line)
    if int(fields["Tgid"]) != pid or int(fields["Pid"]) != tid:
        raise ValueError("task identity differs from identified host PID/TID")
    nspid = [int(v) for v in fields["NSpid"].split()]
    if not nspid or nspid[0] != tid or any(v <= 0 for v in nspid):
        raise ValueError("NSpid missing or not from host proc mount")
    return {"nspid": nspid, "state": fields["State"].strip(), "host_pid": pid, "host_tid": tid}


def parse_futex_wait(text: str) -> dict[str, int] | None:
    """Interpret only Linux x86_64 syscall202 WAIT/WAIT_BITSET, expected2."""
    fields = text.split()
    if len(fields) != 9:
        return None
    try:
        values = [int(v, 0) for v in fields]
    except ValueError:
        return None
    nr, address, operation, expected = values[:4]
    if nr != 202 or operation & ~0x1FF or (operation & 0x7F) not in (0, 9) or expected != 2:
        return None
    if address < 4096 or address >= 1 << 47 or address % 8:
        return None
    return {"syscall_number": nr, "address": address, "operation": operation, "expected": expected}


def _elf64_x86(path: Path) -> bool:
    with path.open("rb") as stream:
        value = stream.read(64)
    return (
        len(value) == 64
        and value[:6] == b"\x7fELF\x02\x01"
        and struct.unpack_from("<H", value, 18)[0] == 62
    )


def _libc_sha(path: Path) -> str:
    # Hash a bounded regular library FILE, never process memory or an arbitrary map.
    size = path.stat().st_size
    if size <= 0 or size > 8 * 1024 * 1024:
        raise ValueError("unsupported libc file size")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for _ in range(9):
            block = stream.read(1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)
    raise ValueError("libc file grew beyond file-read limit")


def _maps(process: Path) -> list[tuple[int, int, str, str]]:
    rows = []
    for line in _text(process / "maps", 256 * 1024).splitlines():
        fields = line.split(maxsplit=5)
        lo, hi = (int(v, 16) for v in fields[0].split("-"))
        rows.append((lo, hi, fields[1], fields[5] if len(fields) == 6 else ""))
    return rows


def _layout(process: Path, maps: list, proofs: list[dict[str, Any]]) -> dict[str, Any]:
    if platform.machine() != "x86_64" or not _elf64_x86(process / "exe"):
        raise ValueError("unsupported target/observer ELF architecture")
    libraries = {p for _, _, _, p in maps if p.startswith("/") and Path(p).name == "libc.so.6"}
    if len(libraries) != 1:
        raise ValueError("target libc file is missing, deleted or ambiguous")
    library = next(iter(libraries))
    actual = _libc_sha(process / "root" / library.lstrip("/"))
    accepted = [p for p in proofs if p.get("libc_sha256") == actual]
    if len(accepted) != 1:
        raise ValueError("no unique measured layout proof for target libc SHA")
    proof = accepted[0]
    if (
        proof.get("architecture") != "x86_64"
        or proof.get("endian") != "little"
        or proof.get("sizeof_mutex") != 40
        or proof.get("alignof_mutex") != 8
        or proof.get("offsets") != OFFSETS
        or proof.get("recursive_kind") != 1
        or proof.get("verification") != "owned-pthread-child-raw-owner-roundtrip"
    ):
        raise ValueError("unsupported or unverified pthread mutex layout")
    return {
        "libc_path": library,
        "libc_sha256": actual,
        "glibc_version": proof.get("glibc_version"),
        "proof_id": proof.get("proof_id"),
        "offsets": OFFSETS,
        "read_bytes": MEMORY_READ_BYTES,
    }


def _check_time(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise TimeoutError("bounded observation elapsed budget exceeded between reads")


def _candidate(
    process: Path,
    main: dict,
    known: list[dict],
    proofs: list[dict],
    deadline: float,
    counters: dict[str, int],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "not_futex_wait",
        "candidate_only": True,
        "node_shared_mutex_verified": False,
        "deadlock_cycle_proven": False,
    }
    try:
        return _read_candidate(process, main, known, proofs, deadline, counters, result)
    except (OSError, ValueError, KeyError, TimeoutError) as error:
        result.update(status="observation_error", error=f"{type(error).__name__}: {error}")
        return result


def _read_candidate(
    process: Path,
    main: dict,
    known: list[dict],
    proofs: list[dict],
    deadline: float,
    counters: dict[str, int],
    result: dict[str, Any],
) -> dict[str, Any]:
    task = process / "task" / str(main["tid"])
    first = _text(task / "syscall", 256)
    wait = parse_futex_wait(first)
    result["syscall_before"] = first
    if wait is None:
        return result
    result.update(candidate_futex_address=hex(wait["address"]), futex=wait)
    state = _status(task / "status", int(main["pid"]), int(main["tid"]))["state"]
    result["main_state_before"] = state
    if state.split()[0] != "S":
        result["status"] = "main_not_sleeping_in_futex_wait"
        return result
    maps = _maps(process)
    result["layout"] = _layout(process, maps, proofs)
    if not any(
        lo <= wait["address"]
        and wait["address"] + MEMORY_READ_BYTES <= hi
        and perms.startswith("rw")
        for lo, hi, perms, _ in maps
    ):
        raise ValueError("candidate does not fit a writable readable mapping")
    _check_time(deadline)
    decoded = []
    fd = os.open(process / "mem", os.O_RDONLY | os.O_CLOEXEC)
    try:
        for _ in range(MEMORY_READS_PER_MAIN):
            _check_time(deadline)
            value = os.pread(fd, MEMORY_READ_BYTES, wait["address"])
            counters["process_memory_bytes"] += len(value)
            counters["process_memory_reads"] += 1
            if len(value) != MEMORY_READ_BYTES:
                raise ValueError("short candidate mutex read")
            lock, count, owner, nusers, kind = struct.unpack("<iIiIi", value)
            decoded.append(
                {"lock": lock, "count": count, "owner": owner, "nusers": nusers, "kind": kind}
            )
            now = _text(task / "syscall", 256)
            if parse_futex_wait(now) != wait:
                result.update(
                    status="changed_during_observation", fields=decoded, syscall_after=now
                )
                return result
    finally:
        os.close(fd)
    result["fields"] = decoded
    state = _status(task / "status", int(main["pid"]), int(main["tid"]))["state"]
    result["main_state_after"] = state
    if state.split()[0] != "S":
        result["status"] = "changed_during_observation"
        return result
    if decoded[0] != decoded[1]:
        result["status"] = "changed_during_observation"
        return result
    fields = decoded[0]
    if (
        fields["lock"] != 2
        or fields["kind"] != 1
        or not 1 <= fields["count"] <= 0xFFFFFFFF
        or fields["owner"] <= 0
        or fields["nusers"] < 1
    ):
        result["status"] = "not_ordinary_recursive_mutex_candidate"
        return result
    owners = [r for r in known if r.get("nspid") and r["nspid"][-1] == fields["owner"]]
    result["owner_namespace_tid_candidate"] = fields["owner"]
    result["status"] = "stable_recursive_mutex_candidate"
    if len(owners) == 1:
        result["owner_host_lwp_candidate"] = owners[0]["host_tid"]
        result["owner_nspid"] = owners[0]["nspid"]
    else:
        result["owner_mapping_error"] = (
            "owner absent or ambiguous in same-process identified task NSpid rows"
        )
    return result


def observe_known_tasks(
    sample: dict[str, Any],
    proofs: list[dict[str, Any]],
    *,
    candidate_pid: int | None = None,
    proc_root: str | Path = "/proc",
) -> dict[str, Any]:
    """Observe only explicit host-proc rows from an existing failure capture.

    This helper is not wired into capture_stall. Missing starttime or a denied
    file becomes observation data; mission logic, deadlines and GDB are absent.
    """
    started = time.monotonic()
    result: dict[str, Any] = {
        "candidate_only": True,
        "capture_wall_time": time.time(),
        "processes": [],
        "limits": {
            "task_rows": MAX_TASK_ROWS,
            "processes": MAX_PROCESSES,
            "gz_main_candidates": MAX_GZ_MAINS,
            "bytes_per_memory_read": MEMORY_READ_BYTES,
            "memory_reads_per_main": MEMORY_READS_PER_MAIN,
            "total_process_memory_bytes_max": MAX_GZ_MAINS
            * MEMORY_READS_PER_MAIN
            * MEMORY_READ_BYTES,
            "elapsed_s_between_reads": MAX_ELAPSED_S,
        },
        "counters": {"process_memory_reads": 0, "process_memory_bytes": 0},
        "errors": [],
    }
    result["candidate_pid"] = candidate_pid
    result["memory_candidate_status"] = (
        "requested" if candidate_pid is not None else "not_requested"
    )
    try:
        if platform.system() != "Linux":
            raise ValueError("Linux procfs observation only")
        source = sample.get("process_threads") or {}
        if source.get("collection_path") != "host_proc":
            raise ValueError("refuse container-local/unknown PID rows against host procfs")
        groups = source.get("threads") or {}
        rows = [{**r, "group": group} for group in ("px4", "gz") for r in groups.get(group, [])]
        if len(rows) > MAX_TASK_ROWS:
            raise ValueError("identified task-row limit exceeded")
        pids = sorted({int(r["pid"]) for r in rows})
        if len(pids) > MAX_PROCESSES:
            raise ValueError("identified process limit exceeded")
        if candidate_pid is not None and (
            type(candidate_pid) is not int
            or candidate_pid <= 0
            or not any(
                r["group"] == "gz"
                and int(r["pid"]) == candidate_pid
                and int(r["tid"]) == candidate_pid
                for r in rows
            )
        ):
            raise ValueError("candidate PID is not an explicitly identified Gazebo main thread")
        deadline = started + MAX_ELAPSED_S
        for pid in pids:
            _check_time(deadline)
            row: dict[str, Any] = {"host_pid": pid, "tasks": [], "errors": []}
            result["processes"].append(row)
            selected = [r for r in rows if int(r["pid"]) == pid]
            mains = [r for r in selected if int(r["tid"]) == pid]
            process_fd = None
            try:
                if (
                    len(mains) != 1
                    or type(mains[0].get("starttime_ticks")) is not int
                    or mains[0]["starttime_ticks"] <= 0
                ):
                    raise ValueError("identified main-thread starttime is missing or ambiguous")
                main = mains[0]
                expected = (pid, main["starttime_ticks"])
                process_fd = os.open(
                    Path(proc_root) / str(pid), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
                )
                # Bind the proc directory inode so a recycled numeric PID cannot redirect reads.
                process = Path("/proc/self/fd") / str(process_fd)
                if _identity(process) != expected:
                    raise ValueError("identified process starttime changed; possible PID reuse")
                for thread in selected:
                    _check_time(deadline)
                    tid = int(thread["tid"])
                    task = process / "task" / str(tid)
                    info: dict[str, Any] = {
                        "host_pid": pid,
                        "host_tid": tid,
                        "group": thread["group"],
                    }
                    try:
                        info.update(_status(task / "status", pid, tid))
                        info["syscall"] = _text(task / "syscall", 256)
                    except (OSError, ValueError, KeyError) as error:
                        info["error"] = f"{type(error).__name__}: {error}"
                    row["tasks"].append(info)
                if main["group"] == "gz" and pid == candidate_pid:
                    try:
                        row["mutex_owner_candidate"] = _candidate(
                            process, main, row["tasks"], proofs, deadline, result["counters"]
                        )
                    except (OSError, ValueError, KeyError, TimeoutError) as error:
                        row["mutex_owner_candidate"] = {
                            "status": "observation_error",
                            "error": f"{type(error).__name__}: {error}",
                            "candidate_only": True,
                        }
                if _identity(process) != expected:
                    row.pop("mutex_owner_candidate", None)
                    raise ValueError("process identity changed while observing; discard candidate")
            except (OSError, ValueError, KeyError, TimeoutError) as error:
                row["errors"].append(f"{type(error).__name__}: {error}")
            finally:
                if process_fd is not None:
                    os.close(process_fd)
    except (OSError, ValueError, KeyError, TimeoutError) as error:
        result["errors"].append(f"{type(error).__name__}: {error}")
    result["elapsed_s"] = time.monotonic() - started
    return result
