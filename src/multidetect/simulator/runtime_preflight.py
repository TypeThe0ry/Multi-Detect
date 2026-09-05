"""Read-only discovery of the external 3-D simulator runtime.

The deterministic simulator and the repository-owned stack planner deliberately
work without PX4/Gazebo installed.  This module answers the next, separate
question: is this host ready to *attempt* a native or Docker-backed external
run?  It never starts a service, creates a container, opens a socket, or
launches PX4/Gazebo.  The resulting document is suitable for attaching to a
field/desktop readiness report and is explicit that process/telemetry/visual
acceptance have not been observed.
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..autonomy.serialization import canonical_sha256
from .isolation import ToolCapability, discover_simulator_tools
from .stack import DEFAULT_REPOSITORY_ROOT, stack_plan_from_scenario

RUNTIME_PREFLIGHT_SCHEMA_VERSION = 1
DOCKER_DIAGNOSTICS_SCHEMA_VERSION = 1
RUNTIME_ASSET_PREFLIGHT_SCHEMA_VERSION = 1
EXTERNAL_RUNTIME_CONTRACT_SCHEMA_VERSION = 1


def _capability_mapping(capability: ToolCapability) -> dict[str, object]:
    """Serialize a :class:`ToolCapability` without relying on ``__dict__``."""

    return {
        "name": capability.name,
        "executable": capability.executable,
        "available": capability.available,
        "version": capability.version,
        "reason": capability.reason,
        "daemon_available": capability.daemon_available,
        "daemon_version": capability.daemon_version,
        "daemon_reason": capability.daemon_reason,
    }


def _probe_wsl() -> dict[str, object]:
    """Return bounded, read-only WSL availability evidence on Windows."""

    if os.name != "nt":
        return {
            "available": False,
            "executable": None,
            "reason": "WSL probe is only applicable on Windows",
            "distros": [],
            "running_distros": 0,
        }

    executable = shutil.which("wsl.exe") or shutil.which("wsl")
    if executable is None:
        return {
            "available": False,
            "executable": None,
            "reason": "wsl.exe not found on PATH",
            "distros": [],
            "running_distros": 0,
        }

    try:
        result = subprocess.run(
            [executable, "--list", "--verbose"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        return {
            "available": False,
            "executable": executable,
            "reason": f"WSL probe failed: {exc.__class__.__name__}",
            "distros": [],
            "running_distros": 0,
        }

    # ``wsl --list`` can emit UTF-16-looking NUL-separated text through a
    # redirected Windows console.  Normalising NULs keeps parsing deterministic
    # without persisting the raw host output in an artifact.
    output = "\n".join(
        line.strip() for line in (result.stdout or "").replace("\x00", "").splitlines()
    )
    distros: list[str] = []
    running = 0
    for line in output.splitlines():
        if not line or line.lower().startswith("name"):
            continue
        # Typical output is ``* Ubuntu Running 2`` or ``Ubuntu Stopped 2``.
        fields = line.split()
        if len(fields) < 2:
            continue
        name_index = 1 if fields[0] == "*" else 0
        if name_index >= len(fields):
            continue
        name = fields[name_index]
        if name.lower() in {"name", "state", "version"}:
            continue
        distros.append(name)
        state_index = name_index + 1
        if state_index < len(fields) and fields[state_index].lower() == "running":
            running += 1
    deduped = sorted(set(distros), key=str.casefold)
    return {
        "available": result.returncode == 0,
        "executable": executable,
        "reason": None
        if result.returncode == 0
        else (next((line for line in output.splitlines() if line), None)
              or "wsl --list --verbose returned a non-zero exit code"),
        "distros": deduped,
        "running_distros": running,
    }


def _probe_docker_desktop_diagnostics() -> dict[str, object]:
    """Read a bounded summary of the Docker Desktop backend log.

    The runtime preflight is deliberately side-effect free.  When the Docker
    client is installed but its daemon probe fails, this helper gives the
    operator a useful, reproducible reason (for example a stale Unix socket)
    without restarting Docker, deleting files, or persisting the whole log.
    Only a small number of lines containing an operational failure marker are
    retained; arbitrary log payloads are never copied into evidence.
    """

    base: dict[str, object] = {
        "schema_version": DOCKER_DIAGNOSTICS_SCHEMA_VERSION,
        "status": "not_available",
        "log_path": None,
        "matched_error_lines": [],
        "likely_cause": None,
        "recommended_action": None,
    }
    if os.name != "nt":
        base["status"] = "not_applicable"
        return base

    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        base["status"] = "path_unavailable"
        base["likely_cause"] = "LOCALAPPDATA is not set"
        return base

    candidates = (
        Path(local_app_data) / "Docker" / "log" / "host" / "com.docker.backend.exe.log",
        Path(local_app_data) / "Docker" / "log" / "host" / "com.docker.backend.exe.log.0",
    )
    log_path = next((path for path in candidates if path.is_file()), None)
    if log_path is None:
        base["status"] = "log_missing"
        base["likely_cause"] = "Docker backend log was not found"
        return base

    base["status"] = "log_read"
    base["log_path"] = str(log_path)
    try:
        # Read only the tail.  Docker Desktop rotates this file, and the tail
        # is sufficient to identify a current startup failure.
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
    except OSError as exc:
        base["status"] = "log_unreadable"
        base["likely_cause"] = f"backend log read failed: {exc.__class__.__name__}"
        return base

    markers = (
        "backend crashed",
        "unexpected error",
        "failed",
        "cannot be accessed by the system",
        "shutting down engines",
        "timeout",
    )
    matches: list[str] = []
    for line in lines:
        compact = " ".join(line.strip().split())
        lowered = compact.casefold()
        if compact and any(marker in lowered for marker in markers):
            # The Windows CLI commonly uses the active GBK code page.  Keep
            # diagnostics printable there even when a backend line contains
            # Unicode symbols (for example ``µ`` in a duration suffix).
            safe = compact.encode("ascii", errors="replace").decode("ascii")
            matches.append(safe[:360])
    # Preserve chronological order while de-duplicating noisy paired entries.
    deduped = list(dict.fromkeys(matches))[-12:]
    base["matched_error_lines"] = deduped
    joined = "\n".join(deduped).casefold()
    if "dockerinference" in joined and "cannot be accessed" in joined:
        base["likely_cause"] = "stale dockerInference reparse/socket entry blocks backend startup"
        base["recommended_action"] = (
            "Exit Docker Desktop completely, remove only the stale dockerInference entry, "
            "then start Docker Desktop and rerun runtime-preflight"
        )
    elif deduped:
        base["likely_cause"] = "Docker Desktop backend reported a startup/runtime error"
        base["recommended_action"] = (
            "Inspect the bounded error lines, repair Docker Desktop, and rerun runtime-preflight"
        )
    else:
        base["likely_cause"] = "No matching startup error was found in the recent backend log"
    return base


def _sha256_file(path: Path) -> str:
    """Hash one already-discovered local file without invoking external tools."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _display_path(path: Path, root: Path | None = None) -> str:
    """Return a stable repository-relative path when one is available."""

    if root is not None:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            pass
    return str(path.resolve())


def _file_provenance(path: Path, *, root: Path | None = None, role: str) -> dict[str, object]:
    """Describe a local asset while keeping missing files explicit."""

    resolved = path.expanduser().resolve(strict=False)
    record: dict[str, object] = {
        "role": role,
        "path": _display_path(resolved, root),
        "absolute_path": str(resolved),
        "exists": resolved.exists(),
        "is_file": resolved.is_file(),
        "is_symlink": path.is_symlink(),
        "sha256": None,
        "size_bytes": None,
    }
    if resolved.is_file() and not path.is_symlink():
        try:
            record["sha256"] = _sha256_file(resolved)
            record["size_bytes"] = resolved.stat().st_size
        except OSError as exc:
            record["read_error"] = f"{exc.__class__.__name__}"
    return record


def _resolve_preflight_path(value: str | Path, *, label: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise TypeError(f"{label} must be a path")
    raw = str(value).strip()
    if not raw:
        raise ValueError(f"{label} must not be empty")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate.resolve(strict=False)


def _infer_px4_workdir(executable: str | None) -> Path | None:
    """Infer a PX4 checkout from a discovered ``bin/px4`` executable."""

    if not executable:
        return None
    candidate = Path(executable).expanduser()
    if not candidate.is_absolute():
        resolved = shutil.which(str(candidate))
        if resolved:
            candidate = Path(resolved)
    if not candidate.is_absolute() or not candidate.is_file():
        return None
    parent = candidate.parent
    if parent.name.casefold() == "bin" and parent.parent.is_dir():
        return parent.parent.resolve()
    return parent.resolve()


def _px4_workdir_preflight(
    *,
    px4: ToolCapability,
    requested: str | Path | None,
) -> dict[str, object]:
    """Check the PX4 relative startup anchor without creating directories."""

    result: dict[str, object] = {
        "requested": requested is not None,
        "status": "not_requested",
        "path": None,
        "absolute_path": None,
        "inferred": False,
        "resolved": False,
        "startup_script": None,
        "reason": "PX4 workdir is checked only for a native external candidate",
    }
    if requested is None and not px4.available:
        return result
    try:
        if requested is not None:
            candidate = _resolve_preflight_path(requested, label="px4_workdir")
        else:
            candidate = _infer_px4_workdir(px4.executable)
            result["inferred"] = candidate is not None
        if candidate is None:
            result.update(
                status="unresolved",
                reason="PX4 workdir could not be inferred from the discovered executable",
            )
            return result
        result["path"] = str(candidate)
        result["absolute_path"] = str(candidate)
        if candidate.is_symlink():
            result.update(status="failed", reason="PX4 workdir must not be a symlink")
            return result
        if not candidate.is_dir():
            result.update(status="failed", reason="PX4 workdir is not a directory")
            return result
        startup = candidate / "etc" / "init.d-posix" / "rcS"
        result["startup_script"] = str(startup)
        if startup.is_symlink() or not startup.is_file():
            result.update(
                status="failed",
                reason="PX4 relative startup script etc/init.d-posix/rcS is missing",
            )
            return result
        result.update(
            status="ready",
            resolved=True,
            reason="PX4 workdir and relative startup script are present",
        )
        return result
    except (OSError, TypeError, ValueError) as exc:
        result.update(
            status="failed",
            reason=f"PX4 workdir probe failed: {exc.__class__.__name__}",
        )
        return result


def _build_asset_preflight(
    *,
    repository_root: str | Path | None,
    scenario: str | Path | None,
    px4_workdir: str | Path | None,
    px4: ToolCapability,
    gazebo: ToolCapability,
) -> dict[str, object]:
    """Resolve optional repository assets without starting a runtime."""

    requested = any(value is not None for value in (repository_root, scenario, px4_workdir))
    result: dict[str, object] = {
        "schema_version": RUNTIME_ASSET_PREFLIGHT_SCHEMA_VERSION,
        "requested": requested,
        "status": "not_requested" if not requested else "pending",
        "asset_gate_passed": False,
        "repository_root": None,
        "scenario": None,
        "stack_plan_sha256": None,
        "assets": {},
        "px4_workdir": None,
        "reason": "scenario/assets were not requested",
    }
    if not requested:
        return result

    try:
        root = (
            _resolve_preflight_path(repository_root, label="repository_root")
            if repository_root is not None
            else DEFAULT_REPOSITORY_ROOT.resolve()
        )
        result["repository_root"] = str(root)
        if root.is_symlink() or not root.is_dir():
            result.update(status="failed", reason="repository_root is not a directory")
            return result
        if px4_workdir is not None:
            result["px4_workdir"] = _file_provenance(
                _resolve_preflight_path(px4_workdir, label="px4_workdir"),
                role="px4_workdir",
            )
        if scenario is None:
            gazebo_root = root / "simulation" / "gazebo"
            ready = gazebo_root.is_dir() and not gazebo_root.is_symlink()
            result.update(
                status="repository_ready" if ready else "failed",
                asset_gate_passed=ready,
                reason=(
                    "repository contains simulation/gazebo assets"
                    if ready
                    else "repository does not contain simulation/gazebo"
                ),
            )
            return result

        scenario_path = _resolve_preflight_path(scenario, label="scenario")
        if not Path(scenario).expanduser().is_absolute():
            scenario_path = (root / Path(scenario).expanduser()).resolve(strict=False)
        plan = stack_plan_from_scenario(
            scenario_path,
            repo_root=root,
            tool_capabilities={"px4": px4.available, "gazebo": gazebo.available},
        )
        result.update(
            status="ready",
            asset_gate_passed=True,
            scenario={
                **_file_provenance(scenario_path, root=root, role="scenario_manifest"),
                "manifest_sha256": plan.scenario_manifest_hash,
                "scenario_id": plan.scenario_id,
                "scenario_version": plan.scenario_version,
            },
            stack_plan_sha256=plan.evidence_hash,
            assets=plan.assets.to_mapping(),
            reason="scenario manifest and repository-owned world/model assets resolved",
        )
        return result
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        result.update(
            status="failed",
            reason=f"asset preflight failed: {exc.__class__.__name__}: {str(exc)[:320]}",
        )
        return result


def _build_external_runtime_contract(
    *,
    native_ready: bool,
    docker_ready: bool,
    execution_mode: str,
    readiness_reason: str,
    asset_preflight: Mapping[str, object],
    px4_workdir: Mapping[str, object],
) -> dict[str, object]:
    """Make the external gate and deterministic fallback boundary explicit."""

    asset_requested = bool(asset_preflight.get("requested", False))
    assets_ready = asset_preflight.get("asset_gate_passed") is True
    toolchain_ready = native_ready or docker_ready
    native_workdir_ready = px4_workdir.get("resolved") is True
    if execution_mode == "native":
        candidate_ready = native_ready and native_workdir_ready
    else:
        candidate_ready = docker_ready if execution_mode == "docker" else False
    if asset_requested:
        candidate_ready = candidate_ready and assets_ready

    if not toolchain_ready:
        reason = readiness_reason
    elif asset_requested and not assets_ready:
        reason = "repository asset preflight failed; external runtime attempt is blocked"
    elif execution_mode == "native" and not native_workdir_ready:
        reason = "native PX4/Gazebo tools exist but PX4 workdir/startup anchor is unresolved"
    else:
        reason = readiness_reason

    return {
        "schema_version": EXTERNAL_RUNTIME_CONTRACT_SCHEMA_VERSION,
        "required_for_real_3d": True,
        "candidate": execution_mode,
        "ready_to_attempt": bool(candidate_ready),
        "reason": reason,
        "gate_passed": False,
        "external_runtime_gate_passed": False,
        "process_started": False,
        "container_started": False,
        "external_io_opened": False,
        "telemetry_observed": False,
        "camera_observed": False,
        "visual_acceptance": "not_claimed",
        "hardware_acceptance": "not_claimed",
        "asset_preflight_requested": asset_requested,
        "asset_preflight_passed": assets_ready if asset_requested else None,
        "px4_workdir_resolved": native_workdir_ready if execution_mode == "native" else None,
        "fallback": {
            "policy": "deterministic_flight_model",
            "automatic": False,
            "used": False,
            "reason": (
                "runtime-preflight never starts or silently switches runtimes; "
                "an orchestrator must record an explicit deterministic fallback"
            ),
        },
        "truth_free": True,
    }


def build_runtime_preflight_document(
    *,
    capabilities: Mapping[str, ToolCapability] | None = None,
    wsl: Mapping[str, object] | None = None,
    docker_diagnostics: Mapping[str, object] | None = None,
    generated_at: _datetime.datetime | None = None,
    repository_root: str | Path | None = None,
    scenario: str | Path | None = None,
    px4_workdir: str | Path | None = None,
) -> dict[str, object]:
    """Build a hash-bound, side-effect-free runtime readiness document.

    ``readiness.ready_to_attempt`` means that the corresponding launcher has a
    usable local toolchain (native PX4/Gazebo) or a responsive Docker daemon.
    It is intentionally not an execution result: no process, container,
    telemetry, camera frame, or desktop rendering is inferred from tool
    availability.  When ``scenario``/``repository_root`` are supplied, the
    optional asset preflight resolves repository-owned world/model files and
    the external contract applies that result before declaring a candidate
    ready.  The deterministic flight model is never selected implicitly.
    """

    auto_discovered = capabilities is None
    selected = dict(capabilities) if capabilities is not None else discover_simulator_tools()
    expected = {"px4", "gazebo", "docker"}
    unknown = set(selected) - expected
    if unknown:
        raise ValueError(f"unknown simulator capability: {', '.join(sorted(unknown))}")
    missing = expected - set(selected)
    if missing:
        raise ValueError(f"missing simulator capability: {', '.join(sorted(missing))}")

    tools = {
        name: _capability_mapping(selected[name]) for name in sorted(expected)
    }
    px4 = selected["px4"]
    gazebo = selected["gazebo"]
    docker = selected["docker"]
    native_ready = bool(px4.available and gazebo.available)
    docker_ready = bool(
        docker.available and docker.daemon_available is True
    )
    if native_ready:
        execution_mode = "native"
        readiness_reason = "PX4 and Gazebo executables are available"
    elif docker_ready:
        execution_mode = "docker"
        readiness_reason = "Docker client and daemon are responsive"
    else:
        execution_mode = "unavailable"
        reasons = []
        if not px4.available:
            reasons.append(f"PX4: {px4.reason or 'executable unavailable'}")
        if not gazebo.available:
            reasons.append(f"Gazebo: {gazebo.reason or 'executable unavailable'}")
        if docker.available and docker.daemon_available is not True:
            reasons.append(
                f"Docker daemon: {docker.daemon_reason or 'not responsive'}"
            )
        elif not docker.available:
            reasons.append(f"Docker: {docker.reason or 'client unavailable'}")
        readiness_reason = "; ".join(reasons) or "no external runtime candidate"

    timestamp = generated_at or _datetime.datetime.now(_datetime.timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=_datetime.timezone.utc)
    timestamp = timestamp.astimezone(_datetime.timezone.utc)
    wsl_document = dict(wsl) if wsl is not None else _probe_wsl()
    if docker_diagnostics is not None:
        docker_diagnostics_document: dict[str, Any] = dict(docker_diagnostics)
    elif auto_discovered and not docker_ready:
        docker_diagnostics_document = _probe_docker_desktop_diagnostics()
    else:
        docker_diagnostics_document = {
            "schema_version": DOCKER_DIAGNOSTICS_SCHEMA_VERSION,
            "status": "not_needed",
            "log_path": None,
            "matched_error_lines": [],
            "likely_cause": None,
            "recommended_action": None,
        }
    asset_preflight = _build_asset_preflight(
        repository_root=repository_root,
        scenario=scenario,
        px4_workdir=px4_workdir,
        px4=px4,
        gazebo=gazebo,
    )
    px4_workdir_preflight = _px4_workdir_preflight(
        px4=px4,
        requested=px4_workdir,
    )
    external_runtime = _build_external_runtime_contract(
        native_ready=native_ready,
        docker_ready=docker_ready,
        execution_mode=execution_mode,
        readiness_reason=readiness_reason,
        asset_preflight=asset_preflight,
        px4_workdir=px4_workdir_preflight,
    )
    document: dict[str, object] = {
        "schema_version": RUNTIME_PREFLIGHT_SCHEMA_VERSION,
        "event": "simulator_runtime_preflight",
        "generated_at_utc": timestamp.isoformat().replace("+00:00", "Z"),
        "host": {
            "os_name": os.name,
            "platform": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
        },
        "tools": tools,
        "wsl": wsl_document,
        "docker_diagnostics": docker_diagnostics_document,
        "asset_preflight": asset_preflight,
        "px4_workdir_preflight": px4_workdir_preflight,
        "external_runtime": external_runtime,
        "readiness": {
            "native_px4_gazebo_ready": native_ready,
            "docker_daemon_ready": docker_ready,
            "ready_to_attempt": native_ready or docker_ready,
            "execution_mode": execution_mode,
            "reason": readiness_reason,
        },
        "claims": {
            "process_started": False,
            "container_started": False,
            "telemetry_observed": False,
            "camera_observed": False,
            "visual_acceptance": "not_claimed",
            "hardware_acceptance": "not_claimed",
        },
        "truth_free": True,
    }
    document["sha256"] = canonical_sha256(document)
    return document


def write_runtime_preflight_document(
    path: str | Path,
    *,
    capabilities: Mapping[str, ToolCapability] | None = None,
    wsl: Mapping[str, object] | None = None,
    docker_diagnostics: Mapping[str, object] | None = None,
    generated_at: _datetime.datetime | None = None,
    repository_root: str | Path | None = None,
    scenario: str | Path | None = None,
    px4_workdir: str | Path | None = None,
) -> Path:
    """Write a runtime preflight document and return its resolved path."""

    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    document = build_runtime_preflight_document(
        capabilities=capabilities,
        wsl=wsl,
        docker_diagnostics=docker_diagnostics,
        generated_at=generated_at,
        repository_root=repository_root,
        scenario=scenario,
        px4_workdir=px4_workdir,
    )
    target.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


__all__ = [
    "DOCKER_DIAGNOSTICS_SCHEMA_VERSION",
    "EXTERNAL_RUNTIME_CONTRACT_SCHEMA_VERSION",
    "RUNTIME_PREFLIGHT_SCHEMA_VERSION",
    "RUNTIME_ASSET_PREFLIGHT_SCHEMA_VERSION",
    "build_runtime_preflight_document",
    "write_runtime_preflight_document",
]
