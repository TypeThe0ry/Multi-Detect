"""Read-only, bounded Docker launcher/context/daemon diagnostics; never repair access."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

CONTRACT = "multidetect.cluster-docker-access-readonly-probe-v1"
MAX_TIMEOUT_S = 5.0
MAX_CAPTURE_BYTES = 8192
MAX_OUTPUT_CHARS = 2000
PROFILE_ERROR = "cannot change profile for the next exec call"
HOST_FORMAT = '{{(index .Endpoints "docker").Host}}'


def redact_text(value: str | bytes) -> str:
    """Keep diagnostic messages, not URL credentials or named authentication values."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if value.lstrip().startswith(("{", "[")):
        return "[structured diagnostic output omitted]"
    if re.search(r"(?i)(?:auth|credential|secret|token)[\"']?\s*[:=]\s*[\[{]", value):
        return "[structured authentication output omitted]"
    value = re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+@", r"\1redacted@", value)
    value = re.sub(r"(?i)\b(bearer|basic)\s+[^\s,;]+", r"\1 [redacted]", value)
    value = re.sub(
        r"(?i)([\"']?(?:[a-z0-9_.-]*[-_])?"
        r"(?:password|passwd|token|secret|api[-_]?key|authorization|credential|auth)"
        r"[\"']?\s*[:=]\s*)(?:[\"'][^\"']*[\"']|[^\s,;&]+)",
        r"\1[redacted]",
        value,
    )
    return value


def _bounded_text(value: str | bytes) -> tuple[str, bool]:
    redacted = redact_text(value)
    return redacted[:MAX_OUTPUT_CHARS], len(redacted) > MAX_OUTPUT_CHARS


def _drain(pipe, buffer: bytearray, state: dict, lock: threading.Lock) -> None:
    try:
        while chunk := pipe.read1(1024):
            with lock:
                room = MAX_CAPTURE_BYTES - len(buffer)
                buffer.extend(chunk[: max(0, room)])
                if len(chunk) > room:
                    state["truncated"] = True
    except (OSError, ValueError):
        pass
    finally:
        pipe.close()


def command_result(command: list[str], timeout_s: float) -> dict:
    """Drain output with a fixed memory cap; kill only this owned diagnostic child."""
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            shell=False,
        )
    except OSError as exc:
        message, truncated = _bounded_text(str(exc))
        return {
            "exit_code": None,
            "succeeded": False,
            "failure": type(exc).__name__,
            "stdout": "",
            "stderr": message,
            "stdout_truncated": False,
            "stderr_truncated": truncated,
            "timed_out": False,
            "elapsed_s": time.monotonic() - started,
        }
    buffers = [bytearray(), bytearray()]
    states = [{"truncated": False}, {"truncated": False}]
    locks = [threading.Lock(), threading.Lock()]
    readers = [
        threading.Thread(target=_drain, args=(pipe, buffer, state, lock), daemon=True)
        for pipe, buffer, state, lock in zip(
            (process.stdout, process.stderr), buffers, states, locks, strict=True
        )
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            pass
    # A launcher descendant may keep its inherited pipe open. Do not wait on it
    # indefinitely or kill unrelated services/process groups to complete a probe.
    for reader in readers:
        reader.join(timeout=0.05)
    output = []
    truncation = []
    for buffer, state, lock in zip(buffers, states, locks, strict=True):
        with lock:
            text, truncated = _bounded_text(bytes(buffer))
            output.append(text)
            truncation.append(state["truncated"] or truncated)
    return {
        "exit_code": None if timed_out else process.returncode,
        "succeeded": not timed_out and process.returncode == 0,
        "failure": "TimeoutExpired" if timed_out else None,
        "stdout": output[0],
        "stderr": output[1],
        "stdout_truncated": truncation[0],
        "stderr_truncated": truncation[1],
        "timed_out": timed_out,
        "elapsed_s": time.monotonic() - started,
    }


def safe_endpoint(host: str) -> str | None:
    """Expose only a Docker Host, removing userinfo/query/fragment entirely."""
    if not isinstance(host, str) or not host or len(host) > MAX_OUTPUT_CHARS:
        return None
    if any(char.isspace() or ord(char) < 32 for char in host):
        return None
    try:
        parts = urlsplit(host)
        if parts.scheme not in {"unix", "npipe", "tcp", "http", "https", "ssh"}:
            return None
        netloc = parts.netloc.rsplit("@", 1)[-1]
        if not parts.netloc:
            return host.split("#", 1)[0].split("?", 1)[0]
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except ValueError:
        return None


def probe(timeout_s: float = 3.0) -> dict:
    if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s):
        raise ValueError("timeout must be finite and between 0.1 and 5 seconds")
    if not 0.1 <= timeout_s <= MAX_TIMEOUT_S:
        raise ValueError("timeout must be finite and between 0.1 and 5 seconds")
    launcher = shutil.which("docker")
    real_path = os.path.realpath(launcher) if launcher is not None else None
    snap_path = bool(
        launcher is not None
        and any(
            path.startswith("/snap/") or path == "/usr/bin/snap" for path in (launcher, real_path)
        )
    )
    report = {
        "schema_version": 1,
        "contract": CONTRACT,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "command_timeout_s": timeout_s,
        "docker_cli_available": launcher is not None,
        "launcher": {
            "which_path": launcher,
            "real_path": real_path,
            "snap_path_evidence": snap_path,
        },
        "client_version": None,
        "context_show": None,
        "context_inspect_host": None,
        "context_name": None,
        "endpoint_host": None,
        "docker_info": None,
        "profile_error_stages": [],
        "diagnosis": "docker_cli_absent",
        "daemon_ready": False,
        "ready": False,
        "image_inspection_performed": False,
        "service_lifecycle_changed": False,
        "gazebo_verified": False,
        "rendering_verified": False,
        "sensor_capture_performed": False,
        "flight_verified": False,
        "training_ready": False,
        "candidate_promoted": False,
        "physical_performance_claimed": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
    }
    if launcher is None:
        return report
    version = command_result([launcher, "--version"], timeout_s)
    report["client_version"] = version
    if version["succeeded"]:
        context = command_result([launcher, "context", "show"], timeout_s)
        report["context_show"] = context
        if context["succeeded"]:
            name = context["stdout"].strip()
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name):
                report["context_name"] = name
                endpoint = command_result(
                    [launcher, "context", "inspect", name, "--format", HOST_FORMAT], timeout_s
                )
                report["context_inspect_host"] = endpoint
                endpoint["profile_error_marker"] = (
                    PROFILE_ERROR in (endpoint["stdout"] + endpoint["stderr"]).lower()
                )
                if endpoint["succeeded"]:
                    report["endpoint_host"] = safe_endpoint(endpoint["stdout"].strip())
                    # Never preserve raw context data, even if a faulty launcher
                    # ignored --format and printed its entire context configuration.
                endpoint["stdout"] = report["endpoint_host"] or "[invalid Host omitted]"
                endpoint["stderr"] = "[context error omitted]" if endpoint["stderr"] else ""
            else:
                context["stdout"] = "[invalid context name omitted]"
    # Always observe the bounded original info command if a launcher exists;
    # failure of --version alone is not proof of a daemon failure.
    daemon = command_result([launcher, "info", "--format", "{{.ServerVersion}}"], timeout_s)
    report["docker_info"] = daemon
    for stage in ("client_version", "context_show", "context_inspect_host", "docker_info"):
        result = report[stage]
        if result and (
            result.get("profile_error_marker")
            or PROFILE_ERROR in (result["stdout"] + result["stderr"]).lower()
        ):
            report["profile_error_stages"].append(stage)
    report["daemon_ready"] = daemon["succeeded"]
    report["ready"] = daemon["succeeded"]
    if daemon["succeeded"]:
        report["diagnosis"] = "daemon_responded_not_gazebo_or_flight_proof"
    elif "client_version" in report["profile_error_stages"]:
        report["diagnosis"] = "launcher_profile_restriction_before_daemon_query"
    elif report["profile_error_stages"]:
        report["diagnosis"] = "profile_restriction_at_command_stage_daemon_state_unknown"
    elif daemon["timed_out"]:
        report["diagnosis"] = "daemon_query_timed_out_state_unknown"
    elif not version["succeeded"]:
        report["diagnosis"] = "launcher_or_client_failed_daemon_not_proven_ready"
    else:
        report["diagnosis"] = "daemon_unreachable_or_access_denied"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=3.0)
    args = parser.parse_args(argv)
    if args.output.exists() or args.output.is_symlink():
        raise FileExistsError(f"refusing to overwrite Docker diagnostic: {args.output}")
    report = probe(args.timeout_s)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0 if report["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
