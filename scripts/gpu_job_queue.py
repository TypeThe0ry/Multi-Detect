#!/usr/bin/env python3
"""Small Linux GPU queue; no third-party dependencies. See docs/gpu-job-queue.md."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ROOT = Path("/data/projects/Multi-Detect-env/runs/jobq")
STATES = ("pending", "running", "done", "failed")
TOKEN_ENV = "MULTIDETECT_JOBQ_TOKEN"
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def write_json(path: Path, value: dict) -> None:
    """Publish whole records atomically, including across abrupt daemon exits."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def file_lock(path: Path, *, blocking: bool = True):
    # Never unlink lock files: replacing an inode would let two owners in.
    with path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise RuntimeError("another queue daemon is running") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def validate_job(value: dict) -> dict:
    allowed = {"name", "gpus", "any_of", "command", "priority", "after"}
    if set(value) - allowed:
        raise ValueError(f"unknown job fields: {sorted(set(value) - allowed)}")
    name = value.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ValueError("name: 1-128 ASCII letters/digits/._-, starting with a letter/digit")
    if ("gpus" in value) == ("any_of" in value):
        raise ValueError("specify exactly one of gpus or any_of")
    devices = value.get("gpus", value.get("any_of"))
    if (not isinstance(devices, list) or not devices
            or any(type(gpu) is not int or gpu < 0 for gpu in devices)
            or len(set(devices)) != len(devices)):
        raise ValueError("GPU indices must be a nonempty list of distinct nonnegative integers")
    command = value.get("command")
    if (not isinstance(command, str) or not Path(command).is_absolute()
            or not Path(command).is_file()):
        raise ValueError("command must be an existing absolute shell script path")
    priority = value.get("priority", 0)
    after = value.get("after", [])
    if type(priority) is not int:
        raise ValueError("priority must be an integer")
    if (not isinstance(after, list)
            or any(not isinstance(dep, str) or not NAME_RE.fullmatch(dep) for dep in after)
            or len(set(after)) != len(after) or name in after):
        raise ValueError("after must contain distinct job names, excluding itself")
    return {**value, "priority": priority, "after": after}


def process_identity(pid: int, proc: Path = Path("/proc")) -> dict | None:
    """Compare boot + start ticks + unguessable launch token, never a command line."""
    try:
        stat = (proc / str(pid) / "stat").read_text(encoding="utf-8")
        fields = stat[stat.rindex(")") + 2:].split()
        if fields[0] in {"Z", "X"}:
            return None
        environ = (proc / str(pid) / "environ").read_bytes().split(b"\0")
        tokens = [entry.split(b"=", 1)[1].decode("ascii") for entry in environ
                  if entry.startswith(f"{TOKEN_ENV}=".encode())]
        if len(tokens) != 1:
            return None
        return {"pid": pid, "start_ticks": int(fields[19]), "token": tokens[0],
                "boot_id": (proc / "sys/kernel/random/boot_id").read_text().strip()}
    except (OSError, ValueError, IndexError, UnicodeError):
        return None


def gpu_memory(runner=subprocess.run) -> dict[int, int]:
    result = runner(
        ["nvidia-smi", "--query-gpu=index,memory.used", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=True, timeout=10,
    )
    memory = {}
    for line in result.stdout.splitlines():
        index, used = (int(part.strip()) for part in line.split(","))
        if index < 0 or used < 0 or index in memory:
            raise ValueError("invalid nvidia-smi memory output")
        memory[index] = used
    if not memory:
        raise ValueError("empty nvidia-smi memory output")
    return memory


class Queue:
    def __init__(self, root: Path, *, threshold: int = 4000, runner=subprocess.run,
                 popen=subprocess.Popen, snapshot=process_identity, sleep=time.sleep):
        if type(threshold) is not int or threshold < 0:
            raise ValueError("memory threshold must be a nonnegative integer (MiB)")
        self.root = root.resolve()
        self.threshold = threshold
        self.runner, self.popen, self.snapshot, self.sleep = runner, popen, snapshot, sleep
        self.children = {}
        self.last_gpu_error = None
        for state in (*STATES, "logs", ".control"):
            (self.root / state).mkdir(parents=True, exist_ok=True)

    def control(self, token: str, suffix: str) -> Path:
        return self.root / ".control" / f"{token}.{suffix}"

    def records(self):
        records = []
        for state in STATES:
            for path in sorted((self.root / state).glob("*.json")):
                if path.is_symlink():
                    raise ValueError(f"queue records must not be symlinks: {path}")
                try:
                    value = read_json(path)
                except (ValueError, UnicodeError) as exc:
                    if state != "pending":
                        raise  # Never guess occupancy from a corrupt running record.
                    value = {"name": path.stem, "invalid_record": str(exc)}
                records.append((state, path, value))
        return records

    def submit(self, value: dict) -> Path:
        job = validate_job(value)
        with file_lock(self.root / ".state.lock"):
            records = self.records()
            if any(record.get("name") == job["name"] or path.stem == job["name"]
                   for _, path, record in records):
                raise ValueError(f"job name already exists: {job['name']}")
            # Missing dependencies are allowed; cycles among known jobs are not.
            graph = {}
            for _, _, record in records:
                name, after = record.get("name"), record.get("after", [])
                if (isinstance(name, str) and isinstance(after, list)
                        and all(isinstance(dep, str) for dep in after)):
                    graph[name] = after
            graph[job["name"]] = job["after"]
            visited, visiting = set(), set()

            def visit(name):
                if name in visiting:
                    raise ValueError("dependency cycle")
                if name in visited:
                    return
                visiting.add(name)
                for dependency in graph.get(name, []):
                    visit(dependency)
                visiting.remove(name)
                visited.add(name)

            for name in graph:
                visit(name)
            path = self.root / "pending" / f"{job['name']}.json"
            write_json(path, job)
            return path

    def finish(self, path: Path, job: dict, *, exit_code=None, error=None, cancelled=False,
               end=None, **extra):
        runtime = job.setdefault("runtime", {})
        runtime.update(extra)
        status = "cancelled" if cancelled else ("done" if exit_code == 0 else "failed")
        runtime.update(end=end or now(), exit=exit_code, error=error, status=status)
        write_json(path, job)
        destination = "done" if runtime["status"] == "done" else "failed"
        os.replace(path, self.root / destination / path.name)

    def cancel(self, name: str) -> str:
        with file_lock(self.root / ".state.lock"):
            matches = [(state, path, job) for state, path, job in self.records()
                       if job.get("name") == name]
            if len(matches) != 1:
                raise ValueError(f"expected one job named {name}")
            state, path, job = matches[0]
            if state == "pending":
                self.finish(path, job, cancelled=True, error="cancelled before launch")
                return "cancelled"
            if state != "running":
                raise ValueError(f"job is already {state}")
            runtime = job["runtime"]
            identity = runtime.get("identity")
            if not identity or self.snapshot(identity["pid"]) != identity:
                raise ValueError("supervisor identity unavailable; no signal sent; audit required")
            # The supervisor alone signals its own Popen child. CLI never kills a PID.
            self.control(runtime["token"], "cancel").touch()
            return "cancellation requested"

    def reconcile(self, path: Path, job: dict) -> None:
        runtime = job.get("runtime", {})
        token = runtime.get("token")
        if not token:
            raise ValueError(f"running record lacks launch token: {path}")
        result = self.control(token, "result.json")
        if result.exists():
            receipt = read_json(result)
            if receipt.get("token") != token:
                raise ValueError("supervisor receipt token mismatch")
            outcome = {key: value for key, value in receipt.items() if key != "token"}
            self.finish(path, job, **outcome)
            return
        identity = runtime.get("identity")
        if identity is None:
            # The worker is gated on its persisted identity, so it cannot run the script.
            self.finish(path, job, error="launch interrupted before identity was committed")
        elif self.snapshot(identity["pid"]) != identity:
            # A killed supervisor may have left descendants: retain the lease, fail closed.
            runtime.update(status="lost",
                           error="supervisor lost; GPU lease retained; manual audit required")
            write_json(path, job)
        elif runtime.get("status") == "lost":
            runtime.update(status="running", error=None)
            write_json(path, job)

    def launch(self, path: Path, job: dict, devices: list[int]) -> None:
        token = uuid.uuid4().hex
        log = self.root / "logs" / f"{job['name']}.{token}.log"
        job["runtime"] = {"status": "starting", "token": token, "gpus": devices,
                          "start": now(), "end": None, "exit": None, "pid": None,
                          "log": str(log)}
        # Move before launching; a restart never silently reruns an ambiguous job.
        write_json(path, job)
        running = self.root / "running" / path.name
        os.replace(path, running)
        env = {**os.environ, TOKEN_ENV: token}
        try:
            with log.open("ab", buffering=0) as stream:
                child = self.popen(
                    [sys.executable, str(Path(__file__).resolve()), "_worker",
                     str(self.root), job["name"], token],
                    stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                    env=env, start_new_session=True, close_fds=True,
                )
            self.children[child.pid] = child
            identity = None
            for _ in range(100):
                identity = self.snapshot(child.pid)
                if identity and identity["token"] == token:
                    break
                identity = None
                self.sleep(0.01)
            if identity is None:
                raise RuntimeError("cannot verify supervisor /proc identity")
            job["runtime"].update(identity=identity, pid=child.pid, status="running")
            write_json(running, job)
        except (OSError, RuntimeError) as exc:
            # No matching persisted identity means the gated worker will exit unused.
            self.finish(running, job, error=f"launch failed: {exc}")

    def tick(self) -> None:
        with file_lock(self.root / ".state.lock"):
            for pid, child in list(self.children.items()):
                if child.poll() is not None:
                    del self.children[pid]
            records = self.records()
            for state, path, job in records:
                if state == "running":
                    self.reconcile(path, job)
            records = self.records()
            pending = []
            for state, path, record in records:
                if state != "pending":
                    continue
                try:
                    job = validate_job(record)
                    if path.stem != job["name"]:
                        raise ValueError("filename must equal name + .json")
                except ValueError as exc:
                    rejected = {"name": path.stem, "invalid_job": record}
                    self.finish(path, rejected, error=f"invalid job: {exc}")
                    continue
                pending.append((path, job))
            records = self.records()
            # Reject ambiguous names even for manually placed files.
            names = [job.get("name") for _, _, job in records]
            if len(set(names)) != len(names):
                raise ValueError("duplicate job names in queue")
            completed = {job["name"] for state, _, job in records
                         if state == "done" and job.get("runtime", {}).get("exit") == 0}
            failed = {job["name"] for state, _, job in records if state == "failed"}
            reserved = {gpu for state, _, job in records if state == "running"
                        for gpu in job["runtime"]["gpus"]}
            # Propagate failure through an entire dependency chain in this tick.
            while True:
                blocked = [(path, job) for path, job in pending
                           if failed.intersection(job["after"])]
                if not blocked:
                    break
                for path, job in blocked:
                    self.finish(path, job, error="dependency failed or cancelled")
                    failed.add(job["name"])
                    pending.remove((path, job))
            if not pending:
                return
            try:
                memory = gpu_memory(self.runner)
                self.last_gpu_error = None
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                message = str(exc)
                if message != self.last_gpu_error:
                    print(f"GPU query failed; no launches: {message}", file=sys.stderr)
                self.last_gpu_error = message
                return
            # Higher priority first; stable name order breaks ties without stat/mtime races.
            ordered = sorted(pending, key=lambda item: (-item[1]["priority"], item[1]["name"]))
            for path, job in ordered:
                if not set(job["after"]).issubset(completed):
                    continue
                candidates = job.get("gpus", job.get("any_of"))
                free = [gpu for gpu in candidates
                        if gpu not in reserved and gpu in memory and memory[gpu] < self.threshold]
                devices = free[:1] if "any_of" in job else candidates
                if not devices or ("gpus" in job and len(free) != len(candidates)):
                    continue
                self.launch(path, job, devices)
                reserved.update(devices)

    def listing(self) -> list[dict]:
        with file_lock(self.root / ".state.lock"):
            records = self.records()
            states = {job.get("name"): state for state, _, job in records}
            return [{**job, "queue_state": state,
                     "waiting_for": [dep for dep in job.get("after", [])
                                     if states.get(dep) != "done"]}
                    for state, _, job in records]


def worker(root: Path, name: str, token: str, *, popen=subprocess.Popen,
           snapshot=process_identity, sleep=time.sleep, monotonic=time.monotonic,
           cancel_grace: float = 10) -> int:
    """Detached supervisor: launch only after daemon commits our exact identity."""
    path = root / "running" / f"{name}.json"
    control = root / ".control"
    identity = snapshot(os.getpid())
    if identity is None or identity["token"] != token:
        return 1
    job = None
    for _ in range(300):
        if not path.exists():
            return 1
        job = read_json(path)
        runtime = job.get("runtime", {})
        if runtime.get("token") != token:
            return 1
        if runtime.get("identity") == identity:
            break
        sleep(0.1)
    else:
        return 1
    cancelled = False
    terminating_at = None
    receipt = {"token": token, "exit_code": None}
    if (control / f"{token}.cancel").exists():
        receipt.update(end=now(), cancelled=True, error="cancelled before script launch")
        write_json(control / f"{token}.result.json", receipt)
        return 0
    try:
        # CUDA_DEVICE_ORDER makes integer visibility align with nvidia-smi's PCI order.
        env = {**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
               "CUDA_VISIBLE_DEVICES": ",".join(map(str, job["runtime"]["gpus"]))}
        child = popen(["/bin/bash", job["command"]], stdin=subprocess.DEVNULL,
                      env=env, cwd=str(Path(job["command"]).parent), close_fds=True,
                      start_new_session=True)
        receipt["script_pid"] = child.pid
        receipt["script_start"] = now()
        try:
            write_json(control / f"{token}.child.json",
                       {"pid": child.pid, "start": receipt["script_start"],
                        "identity": snapshot(child.pid)})
            while child.poll() is None:
                if (control / f"{token}.cancel").exists() and not cancelled:
                    cancelled = True
                    terminating_at = monotonic()
                    child.terminate()
                elif terminating_at is not None and monotonic() - terminating_at >= cancel_grace:
                    child.kill()
                    terminating_at = None
                sleep(0.2)
            receipt["exit_code"] = child.wait()
        finally:
            # Also clean up our child if local receipt I/O fails. Never signal by name/group.
            if child.poll() is None:
                child.kill()
                child.wait()
    except OSError as exc:
        receipt["error"] = str(exc)
    receipt.update(end=now(), cancelled=cancelled)
    write_json(control / f"{token}.result.json", receipt)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue-dir", type=Path, default=DEFAULT_ROOT)
    sub = parser.add_subparsers(dest="action", required=True)
    run = sub.add_parser("run", help="run in foreground; SIGINT/SIGTERM stops only scheduling")
    run.add_argument("--memory-threshold-mib", type=int, default=4000)
    run.add_argument("--poll-seconds", type=float, default=5)
    submit = sub.add_parser("submit", help="validate and submit one JSON file")
    submit.add_argument("job", type=Path)
    sub.add_parser("list", help="print all records as JSON")
    cancel = sub.add_parser("cancel", help="cancel pending job or ask its supervisor to stop")
    cancel.add_argument("name")
    internal = sub.add_parser("_worker", help="internal supervisor, started by run")
    internal.add_argument("root", type=Path)
    internal.add_argument("name")
    internal.add_argument("token")
    args = parser.parse_args(argv)
    try:
        if args.action == "_worker":
            return worker(args.root, args.name, args.token)
        queue = Queue(args.queue_dir, threshold=getattr(args, "memory_threshold_mib", 4000))
        if args.action == "submit":
            print(queue.submit(read_json(args.job)))
        elif args.action == "list":
            print(json.dumps(queue.listing(), ensure_ascii=False, indent=2))
        elif args.action == "cancel":
            print(queue.cancel(args.name))
        else:
            if not math.isfinite(args.poll_seconds) or args.poll_seconds <= 0:
                raise ValueError("poll-seconds must be finite and positive")
            stopping = False

            def stop(_signum, _frame):
                nonlocal stopping
                stopping = True

            with file_lock(queue.root / ".daemon.lock", blocking=False):
                signal.signal(signal.SIGINT, stop)
                signal.signal(signal.SIGTERM, stop)
                while not stopping:
                    queue.tick()
                    if not stopping:
                        time.sleep(args.poll_seconds)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"gpu-job-queue: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
