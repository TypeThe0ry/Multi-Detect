"""Pure, dry-run-safe launcher planning for the repository-owned SIM stack.

``multidetect.simulator.stack`` resolves a scenario into a hash-bound
``SimulatorStackPlan``.  This module is the next (deliberately separate)
boundary: it turns that plan into two concrete process specifications for
PX4 SITL and Gazebo, chooses the working directories and log files, and
records enough evidence for a later operator-owned process runner.

There is intentionally **no process execution code** here.  In particular,
this module does not import or call :mod:`subprocess`, open sockets, or probe
the simulator.  ``SimulatorExternalStackLauncher.start`` is an explicit
dry-run alias which returns the prepared plan; this makes accidental startup
from a test, GUI, or evidence command impossible.

The strict boundary is useful for both interactive QGC work and CI:

* PX4's relative ``etc/init.d-posix/rcS`` is checked against an explicit
  ``PX4_WORKDIR`` (or a safely inferred conventional ``bin/px4`` layout).
* Environment values are copied from the hash-bound stack plan and endpoint
  identity/ports cannot be overridden.
* The canonical startup order is PX4 first, then Gazebo.  QGC is owned by the
  higher-level orchestrator and is intentionally not accepted here.
* Log paths are unique, regular-file destinations below the job directory;
  no shell redirection is represented in a command string.

Missing external binaries or an unresolved PX4 workdir are *advisory* in a
dry-run, so a developer can generate deterministic JSON on a clean checkout.
They become hard errors when ``dry_run=False`` is requested.  Either mode
still reports ``external_processes_started=False``.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..autonomy.serialization import canonical_sha256
from .px4_launch import Px4LaunchError, Px4LaunchPlan, build_px4_launch_plan
from .stack import (
    LOOPBACK_HOSTS,
    SimulatorStackPlan,
)

SCHEMA_VERSION = 1
DEFAULT_PROCESS_ORDER = ("px4", "gazebo")
DEFAULT_LOG_DIRECTORY_NAME = "logs"
DEFAULT_EVIDENCE_NAME = "external-stack.json"

_SAFE_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_NETWORK_URI = re.compile(r"(?:https?|ftp|fuel|tcp|ws|wss)://", re.IGNORECASE)
_SERIAL_PREFIX = re.compile(r"(?:^|[\s,;])(?:serial:|com\d+|\\\\\.\\com|/dev/tty)", re.IGNORECASE)
_UDP_ENDPOINT = re.compile(
    r"^(?:udp(?:out)?):(?P<host>\[[^\]]+\]|[^:]+):(?P<port>\d{1,5})$",
    re.IGNORECASE,
)
_STARTUP_SWITCHES = frozenset({"-s", "--startup-script", "--startup"})
_LOCKED_ENV_KEYS = frozenset(
    {
        "PX4_SIM_HOST",
        "PX4_MAVLINK_UDP_PORT",
        "PX4_MAVLINK_TELEMETRY_PORT",
        "MD_SIM_SCENARIO_ID",
        "MD_SIM_SCENARIO_SHA256",
        "MD_SIM_SEED",
        "PX4_SIM_MODEL",
        "PX4_SYS_AUTOSTART",
        "PX4_GZ_WORLD",
        "PX4_GZ_MODELS",
        "GZ_SIM_RESOURCE_PATH",
    }
)
_ADVISORY_VALIDATION_KEYS = frozenset(
    {
        "tools_available",
        "px4_workdir_resolved",
        "external_processes_started",
        "external_io_opened",
        # A dry-run wrapper may intentionally use the generated template when
        # PX4 is absent.  The separate ``ready_to_launch`` property still
        # reports false; this key only keeps policy/gating evidence readable.
        "px4_launch_ready",
    }
)


class ExternalStackError(RuntimeError):
    """Raised when an external SIM process plan violates the SIM boundary."""


# Names used by downstream integrations in earlier prototype branches.
SimulatorExternalStackError = ExternalStackError
ExternalStackValidationError = ExternalStackError


def _as_absolute_path(value: str | Path, *, label: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise ExternalStackError(f"{label} must be a path")
    raw = str(value).strip()
    if not raw:
        raise ExternalStackError(f"{label} must not be empty")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve(strict=False)


def _assert_directory(path: Path, *, label: str, create: bool = False) -> Path:
    """Validate a directory without following a symlink at the leaf."""

    if path.exists() and path.is_symlink():
        raise ExternalStackError(f"{label} must not be a symlink: {path}")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ExternalStackError(f"{label} is not a directory: {path}")
    return path.resolve()


def _assert_contained(path: Path, root: Path, *, label: str) -> None:
    try:
        path.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise ExternalStackError(f"{label} escapes job directory: {path}") from exc


def _normalise_command(command: Sequence[str], *, label: str) -> tuple[str, ...]:
    if isinstance(command, (str, bytes, bytearray)) or not isinstance(command, Sequence):
        raise ExternalStackError(f"{label} must be a sequence of argv tokens")
    result = tuple(str(token) for token in command)
    if not result:
        raise ExternalStackError(f"{label} must not be empty")
    for token in result:
        if not token or "\x00" in token or "\r" in token or "\n" in token:
            raise ExternalStackError(f"{label} contains an invalid argv token")
    return result


def _normalise_environment(
    value: Mapping[str, object] | Sequence[Sequence[object]], *, label: str
) -> dict[str, str]:
    if isinstance(value, Mapping):
        items = value.items()
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = value
    else:
        raise ExternalStackError(f"{label} must be a mapping or key/value sequence")

    result: dict[str, str] = {}
    for item in items:
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes, bytearray)):
            raise ExternalStackError(f"{label} entries must be key/value pairs")
        if len(item) != 2:
            raise ExternalStackError(f"{label} entries must have two values")
        key, raw_value = item
        if not isinstance(key, str) or _SAFE_ENV_KEY.fullmatch(key) is None:
            raise ExternalStackError(f"{label} contains an invalid key: {key!r}")
        if not isinstance(raw_value, str):
            raw_value = str(raw_value)
        if "\x00" in raw_value or "\r" in raw_value or "\n" in raw_value:
            raise ExternalStackError(f"{label} contains a control character in {key}")
        if key in result:
            raise ExternalStackError(f"{label} contains duplicate key: {key}")
        result[key] = raw_value
    return result


def _loopback(value: str) -> bool:
    return value in LOOPBACK_HOSTS


def _parse_port(value: str, *, label: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ExternalStackError(f"{label} must be an integer port") from exc
    if not 1 <= port <= 65535:
        raise ExternalStackError(f"{label} must be in 1..65535")
    return port


def _command_executable_matches(command: Sequence[str], expected: str, *, label: str) -> None:
    observed_path = Path(command[0]).expanduser()
    expected_path = Path(expected).expanduser()
    if expected_path.is_absolute():
        matches = observed_path.is_absolute() and observed_path.resolve(
            strict=False
        ) == expected_path.resolve(strict=False)
    else:
        # A bare planner command must stay bare.  Accepting an arbitrary
        # absolute path with the same basename would silently redirect the
        # future runner to a different executable.
        matches = command[0].strip().lower() == str(expected).strip().lower()
    if not matches:
        raise ExternalStackError(
            f"{label} executable conflicts with stack plan: expected {expected!r}, "
            f"got {command[0]!r}"
        )


def _unsafe_endpoint_token(value: str) -> bool:
    """Return whether a command/env token names a non-loopback endpoint."""

    if _NETWORK_URI.search(value) or _SERIAL_PREFIX.search(value):
        return True
    if value.lower().startswith("udp://"):
        parsed = urlparse(value)
        return parsed.hostname not in LOOPBACK_HOSTS
    match = _UDP_ENDPOINT.fullmatch(value)
    if match is None:
        return value.lower().startswith("udpout:") or value.lower().startswith("udp:")
    host = match.group("host")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host not in LOOPBACK_HOSTS


def _startup_script(command: Sequence[str]) -> tuple[str, bool] | None:
    for index, token in enumerate(command[:-1]):
        if token in _STARTUP_SWITCHES:
            script = command[index + 1]
            return script, Path(script).is_absolute()
    return None


def _candidate_px4_workdir(plan: SimulatorStackPlan) -> Path | None:
    """Infer ``<build>/`` from an absolute ``<build>/bin/px4`` executable."""

    executable = Path(plan.px4_executable).expanduser()
    if not executable.is_absolute():
        resolved = shutil.which(str(executable))
        if resolved:
            executable = Path(resolved)
    if not executable.is_absolute() or not executable.is_file():
        return None
    parent = executable.parent
    if parent.name.lower() == "bin" and parent.parent.is_dir():
        return parent.parent.resolve()
    return parent.resolve()


def _resolve_px4_workdir(
    plan: SimulatorStackPlan,
    *,
    requested: str | Path | None,
    dry_run: bool,
) -> tuple[Path | None, bool, str | None]:
    """Return ``(cwd, resolved, reason)`` for the PX4 process."""

    environment = plan.environment_mapping
    configured = environment.get("PX4_WORKDIR")
    explicit = None if requested is None else _as_absolute_path(requested, label="px4_workdir")
    from_environment = (
        None if configured is None else _as_absolute_path(configured, label="PX4_WORKDIR")
    )
    if explicit is not None and from_environment is not None and explicit != from_environment:
        raise ExternalStackError(
            f"px4_workdir conflicts with plan PX4_WORKDIR: {explicit} != {from_environment}"
        )
    candidate = explicit or from_environment or _candidate_px4_workdir(plan)
    startup = _startup_script(plan.px4_command)
    requires_relative_startup = startup is not None and not startup[1]

    if candidate is None:
        if requires_relative_startup:
            reason = "PX4_WORKDIR unresolved for relative startup script"
            if not dry_run:
                raise ExternalStackError(reason)
            return None, False, reason
        return None, True, None

    # An explicitly supplied path is a hard contract even during dry-run.
    if candidate.exists() and candidate.is_symlink():
        raise ExternalStackError(f"PX4 workdir must not be a symlink: {candidate}")
    if not candidate.is_dir():
        raise ExternalStackError(f"PX4 workdir is not a directory: {candidate}")
    candidate = candidate.resolve()

    if startup is not None:
        script, absolute = startup
        startup_path = Path(script) if absolute else candidate / script
        if not startup_path.is_file():
            reason = f"PX4 startup script not found: {startup_path}"
            if not dry_run:
                raise ExternalStackError(reason)
            # Explicit paths are still rejected: a dry-run with a real path
            # typo must not look launchable.  Only an entirely unresolved
            # inferred workdir is advisory.
            if explicit is not None or from_environment is not None:
                raise ExternalStackError(reason)
            return candidate, False, reason
    return candidate, True, None


def _validate_environment(
    plan: SimulatorStackPlan,
    environment: Mapping[str, str],
    *,
    px4_workdir: Path | None,
) -> None:
    """Validate endpoint identity and reject unsafe env overrides."""

    required = {
        "PX4_SIM_HOST",
        "PX4_MAVLINK_UDP_PORT",
        "PX4_MAVLINK_TELEMETRY_PORT",
        "MD_SIM_SCENARIO_ID",
        "MD_SIM_SCENARIO_SHA256",
        "MD_SIM_SEED",
    }
    missing = sorted(required - set(environment))
    if missing:
        raise ExternalStackError(
            "stack plan environment is missing required key(s): " + ", ".join(missing)
        )
    host = environment.get("PX4_SIM_HOST")
    if host is not None and not _loopback(host):
        raise ExternalStackError("PX4_SIM_HOST must be literal loopback")
    for key, expected in (
        ("PX4_MAVLINK_UDP_PORT", plan.ports.px4_control_port),
        ("PX4_MAVLINK_TELEMETRY_PORT", plan.ports.px4_telemetry_port),
    ):
        if key in environment and _parse_port(environment[key], label=key) != expected:
            raise ExternalStackError(f"{key} conflicts with stack plan port {expected}")

    if environment["MD_SIM_SCENARIO_ID"] != plan.scenario_id:
        raise ExternalStackError("MD_SIM_SCENARIO_ID conflicts with stack plan")
    if environment["MD_SIM_SCENARIO_SHA256"] != plan.scenario_manifest_hash:
        raise ExternalStackError("MD_SIM_SCENARIO_SHA256 conflicts with stack plan")
    if environment["MD_SIM_SEED"] != str(plan.seed):
        raise ExternalStackError("MD_SIM_SEED conflicts with stack plan")

    for key, value in environment.items():
        if _unsafe_endpoint_token(value):
            raise ExternalStackError(f"SIM environment {key} contains a non-local endpoint")
    if px4_workdir is not None and "PX4_WORKDIR" in environment:
        configured = _as_absolute_path(environment["PX4_WORKDIR"], label="PX4_WORKDIR")
        if configured != px4_workdir:
            raise ExternalStackError("PX4_WORKDIR does not match selected PX4 cwd")


def _tool_available(plan: SimulatorStackPlan, name: str) -> bool:
    capabilities = dict(plan.tool_capabilities)
    if name in capabilities:
        return bool(capabilities[name])
    executable = plan.px4_executable if name == "px4" else plan.gazebo_executable
    path = Path(executable).expanduser()
    return path.is_file() if path.is_absolute() else shutil.which(str(executable)) is not None


def _validate_log_destination(path: Path, *, job_dir: Path) -> None:
    _assert_contained(path, job_dir, label="log path")
    if path.name in {".", ".."} or path.suffix.lower() != ".log":
        raise ExternalStackError(f"log path must end in .log: {path}")
    if path.exists() and path.is_symlink():
        raise ExternalStackError(f"log path must not be a symlink: {path}")
    if path.exists() and path.is_dir():
        raise ExternalStackError(f"log path is a directory: {path}")


@dataclass(frozen=True, slots=True)
class ExternalProcessSpec:
    """One argv/cwd/env/log tuple for a future process runner."""

    name: str
    command: tuple[str, ...] | Sequence[str]
    cwd: str | Path
    env: Mapping[str, object] | Sequence[Sequence[object]] = ()
    log_path: str | Path = "process.log"
    order: int = 0
    required: bool = True
    depends_on: tuple[str, ...] | Sequence[str] = ()

    def __post_init__(self) -> None:
        if self.name not in DEFAULT_PROCESS_ORDER:
            raise ExternalStackError(f"unsupported external SIM process: {self.name!r}")
        object.__setattr__(
            self,
            "command",
            _normalise_command(self.command, label=f"{self.name} command"),
        )
        object.__setattr__(self, "cwd", _as_absolute_path(self.cwd, label=f"{self.name} cwd"))
        object.__setattr__(
            self,
            "env",
            dict(sorted(_normalise_environment(self.env, label=f"{self.name} env").items())),
        )
        object.__setattr__(
            self,
            "log_path",
            _as_absolute_path(self.log_path, label=f"{self.name} log_path"),
        )
        if isinstance(self.order, bool) or not isinstance(self.order, int) or self.order < 0:
            raise ExternalStackError(f"{self.name} order must be a non-negative integer")
        if not isinstance(self.required, bool):
            raise ExternalStackError(f"{self.name} required must be bool")
        dependencies = tuple(str(item).lower() for item in self.depends_on)
        if len(dependencies) != len(set(dependencies)):
            raise ExternalStackError(f"{self.name} dependencies must be unique")
        if any(item not in DEFAULT_PROCESS_ORDER or item == self.name for item in dependencies):
            raise ExternalStackError(f"{self.name} dependencies contain an invalid process")
        object.__setattr__(self, "depends_on", dependencies)

    @property
    def argv(self) -> tuple[str, ...]:
        return tuple(self.command)

    @property
    def environment(self) -> dict[str, str]:
        return dict(self.env)

    @property
    def log(self) -> Path:
        return self.log_path

    @property
    def shell(self) -> bool:
        return False

    @property
    def stderr(self) -> str:
        return "stdout"

    def to_mapping(self, *, job_dir: Path | None = None) -> dict[str, object]:
        def display(path: Path) -> str:
            if job_dir is not None:
                try:
                    return str(path.relative_to(job_dir))
                except ValueError:
                    pass
            return str(path)

        return {
            "name": self.name,
            "order": self.order,
            "required": self.required,
            "depends_on": list(self.depends_on),
            "command": list(self.command),
            "cwd": str(self.cwd),
            "env": dict(self.env),
            "log": display(self.log_path),
            "log_absolute_path": str(self.log_path),
            "shell": False,
            "stderr": "stdout",
            "encoding": "utf-8",
        }


@dataclass(frozen=True, slots=True)
class ExternalStackPlan:
    """A no-side-effect PX4/Gazebo execution plan and its evidence."""

    stack_plan: SimulatorStackPlan
    job_dir: Path
    processes: tuple[ExternalProcessSpec, ...]
    dry_run: bool = True
    process_order: tuple[str, ...] = DEFAULT_PROCESS_ORDER
    px4_workdir: Path | None = None
    px4_workdir_resolved: bool = False
    inherit_environment: bool = False
    log_directory: Path | None = None
    validation: tuple[tuple[str, bool], ...] = ()
    schema_version: int = SCHEMA_VERSION
    # Optional job-local patched PX4 startup wrapper.  It is opt-in to keep
    # the historical no-launch process-spec contract byte-for-byte compatible.
    px4_launch: Px4LaunchPlan | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stack_plan, SimulatorStackPlan):
            raise TypeError("stack_plan must be SimulatorStackPlan")
        root = _as_absolute_path(self.job_dir, label="job_dir")
        object.__setattr__(self, "job_dir", root)
        if self.px4_workdir is not None:
            object.__setattr__(
                self,
                "px4_workdir",
                _as_absolute_path(self.px4_workdir, label="px4_workdir"),
            )
        if self.log_directory is not None:
            object.__setattr__(
                self,
                "log_directory",
                _as_absolute_path(self.log_directory, label="log_directory"),
            )
            _assert_contained(self.log_directory, root, label="log_directory")
        order = tuple(self.process_order)
        if order != DEFAULT_PROCESS_ORDER:
            raise ExternalStackError(
                f"external SIM process order must be {DEFAULT_PROCESS_ORDER}, got {order}"
            )
        if len(self.processes) != len(order):
            raise ExternalStackError("external SIM plan must contain exactly PX4 and Gazebo")
        observed = tuple(item.name for item in self.processes)
        if observed != order:
            raise ExternalStackError(
                f"process specs are out of order: expected {order}, got {observed}"
            )
        if any(item.order != index for index, item in enumerate(self.processes)):
            raise ExternalStackError("process order indexes must be contiguous from zero")
        expected_dependencies = ((), ("px4",))
        for item, expected in zip(self.processes, expected_dependencies, strict=True):
            if tuple(item.depends_on) != expected:
                raise ExternalStackError(
                    f"{item.name} dependencies must be {expected}, got {item.depends_on}"
                )
        keys: set[str] = set()
        for key, value in self.validation:
            if key in keys or not isinstance(key, str) or not isinstance(value, bool):
                raise ExternalStackError("validation entries must be unique string/bool pairs")
            keys.add(key)
        if self.schema_version != SCHEMA_VERSION:
            raise ExternalStackError(f"unsupported external stack schema {self.schema_version}")
        if self.px4_launch is not None:
            if self.px4_launch.stack_plan.evidence_hash != self.stack_plan.evidence_hash:
                raise ExternalStackError("PX4 launch plan does not match source stack plan")
            try:
                self.px4_launch.job_dir.relative_to(root)
            except ValueError as exc:
                raise ExternalStackError("PX4 launch job directory escapes job_dir") from exc
            try:
                self.px4_launch.wrapper_path.relative_to(root)
            except ValueError as exc:
                raise ExternalStackError("PX4 launch wrapper escapes job_dir") from exc

    @property
    def source_plan(self) -> SimulatorStackPlan:
        return self.stack_plan

    @property
    def source_stack_plan_sha256(self) -> str:
        return self.stack_plan.evidence_hash

    @property
    def launch_order(self) -> tuple[str, ...]:
        return self.process_order

    @property
    def log_dir(self) -> Path:
        return self.log_directory or (self.job_dir / DEFAULT_LOG_DIRECTORY_NAME)

    @property
    def environment(self) -> dict[str, str]:
        return self.px4.environment

    @property
    def gate_passed(self) -> bool:
        checks = dict(self.validation)
        required = {
            key: value for key, value in checks.items() if key not in _ADVISORY_VALIDATION_KEYS
        }
        return bool(required) and all(required.values())

    @property
    def scenario_id(self) -> str:
        return self.stack_plan.scenario_id

    @property
    def ready_to_launch(self) -> bool:
        checks = dict(self.validation)
        ready = bool(
            checks.get("tools_available", False) and checks.get("px4_workdir_resolved", False)
        )
        if self.px4_launch is not None:
            ready = ready and self.px4_launch.ready_to_launch
        return ready

    @property
    def px4(self) -> ExternalProcessSpec:
        return self.processes[0]

    @property
    def gazebo(self) -> ExternalProcessSpec:
        return self.processes[1]

    @property
    def commands(self) -> dict[str, list[str]]:
        return {item.name: list(item.command) for item in self.processes}

    def to_mapping(self) -> dict[str, object]:
        checks = dict(self.validation)
        return {
            "schema_version": self.schema_version,
            "event": "simulator_external_stack_plan",
            "scenario_id": self.stack_plan.scenario_id,
            "scenario_manifest_hash": self.stack_plan.scenario_manifest_hash,
            "source_stack_plan_sha256": self.source_stack_plan_sha256,
            "job_dir": str(self.job_dir),
            "dry_run": self.dry_run,
            "process_order": list(self.process_order),
            "launch_order": list(self.process_order),
            "log_dir": str(self.log_dir),
            "processes": [item.to_mapping(job_dir=self.job_dir) for item in self.processes],
            "commands": self.commands,
            "px4_workdir": None if self.px4_workdir is None else str(self.px4_workdir),
            "px4_workdir_resolved": self.px4_workdir_resolved,
            "inherit_environment": self.inherit_environment,
            "px4_launch": (
                None if self.px4_launch is None else self.px4_launch.to_mapping_with_hash()
            ),
            "px4_launch_sha256": (
                None if self.px4_launch is None else self.px4_launch.evidence_hash
            ),
            "source_stack_plan": self.stack_plan.to_mapping_with_hash(),
            "validation": {
                **checks,
                "gate_passed": self.gate_passed,
                "ready_to_launch": self.ready_to_launch,
                "external_processes_started": False,
                "external_io_opened": False,
                "loopback_only": True,
            },
            "readiness": {
                "tools_available": checks.get("tools_available", False),
                "px4_workdir_resolved": checks.get("px4_workdir_resolved", False),
                "ready_to_launch": self.ready_to_launch,
            },
            "runtime": {
                "external_processes_started": False,
                "external_io_opened": False,
                "shell": False,
                "truth_free": True,
            },
            "truth_free": True,
        }

    @property
    def evidence_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @property
    def evidence(self) -> dict[str, object]:
        return self.to_mapping_with_hash()

    def to_mapping_with_hash(self) -> dict[str, object]:
        result = self.to_mapping()
        result["sha256"] = self.evidence_hash
        return result

    def as_dict(self) -> dict[str, object]:
        return self.to_mapping_with_hash()

    def write_json(self, path: str | Path, *, overwrite: bool = True) -> Path:
        target = _as_absolute_path(path, label="evidence path")
        if target.exists() and not overwrite:
            raise ExternalStackError(f"evidence path already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(
            json.dumps(self.to_mapping_with_hash(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
        return target


def build_external_stack_plan(
    stack_plan: SimulatorStackPlan,
    *,
    job_dir: str | Path | None = None,
    px4_workdir: str | Path | None = None,
    process_order: Sequence[str] = DEFAULT_PROCESS_ORDER,
    env_overrides: Mapping[str, object] | None = None,
    inherit_environment: bool = False,
    log_dir: str | Path | None = None,
    dry_run: bool = True,
    strict: bool | None = None,
    px4_launch_enabled: bool = False,
    px4_launch_strict: bool | None = None,
) -> ExternalStackPlan:
    """Turn a :class:`SimulatorStackPlan` into a no-launch process plan.

    ``dry_run=True`` (the default) permits absent PX4/Gazebo binaries and an
    unresolvable conventional PX4 checkout, but records those as readiness
    failures.  Explicit malformed paths, unsafe endpoints, bad environment
    values, and an invalid process order are always rejected.  Set
    ``dry_run=False`` to make tool/workdir readiness hard errors; this still
    only creates a plan and never starts a process.
    """

    if not isinstance(stack_plan, SimulatorStackPlan):
        raise TypeError("stack_plan must be SimulatorStackPlan")
    if not isinstance(dry_run, bool):
        raise TypeError("dry_run must be bool")
    if not isinstance(inherit_environment, bool):
        raise TypeError("inherit_environment must be bool")
    if not isinstance(px4_launch_enabled, bool):
        raise TypeError("px4_launch_enabled must be bool")
    if px4_launch_strict is not None and not isinstance(px4_launch_strict, bool):
        raise TypeError("px4_launch_strict must be bool or None")
    if strict is None:
        strict = not dry_run
    if not isinstance(strict, bool):
        raise TypeError("strict must be bool or None")

    order = tuple(str(item).lower() for item in process_order)
    if order != DEFAULT_PROCESS_ORDER:
        raise ExternalStackError(
            f"external SIM process order must be {DEFAULT_PROCESS_ORDER}, got {order}"
        )

    if job_dir is None:
        selected_job_dir = (
            stack_plan.repository_root
            / "artifacts"
            / "evaluation"
            / (f"external-stack-{stack_plan.scenario_id}")
        )
    else:
        selected_job_dir = _as_absolute_path(job_dir, label="job_dir")
    selected_job_dir = _assert_directory(selected_job_dir, label="job_dir", create=True)
    if log_dir is None:
        selected_log_dir = selected_job_dir / DEFAULT_LOG_DIRECTORY_NAME
    else:
        selected_log_dir = _as_absolute_path(log_dir, label="log_dir")
    _assert_contained(selected_log_dir, selected_job_dir, label="log_dir")
    selected_log_dir = _assert_directory(selected_log_dir, label="log_dir", create=True)

    selected_px4_cwd, workdir_ok, workdir_reason = _resolve_px4_workdir(
        stack_plan,
        requested=px4_workdir,
        dry_run=dry_run,
    )
    if selected_px4_cwd is None:
        # A dry-run plan still needs a concrete, safe cwd for evidence.  It
        # points at the disposable job directory and is never launched.
        selected_px4_cwd = selected_job_dir
    selected_px4_cwd = _assert_directory(selected_px4_cwd, label="PX4 cwd", create=True)
    gazebo_cwd = _assert_directory(stack_plan.repository_root, label="Gazebo cwd")

    environment = _normalise_environment(
        stack_plan.environment_mapping,
        label="stack plan environment",
    )
    if env_overrides is not None:
        overrides = _normalise_environment(env_overrides, label="env_overrides")
        for key, value in overrides.items():
            if key in _LOCKED_ENV_KEYS and key in environment and environment[key] != value:
                raise ExternalStackError(f"env_overrides cannot change bound key {key}")
            environment[key] = value
    # Make the selected cwd explicit in the child environment.  This is
    # especially important when the command uses a relative ``rcS`` path.
    if workdir_ok:
        configured = environment.get("PX4_WORKDIR")
        if (
            configured is not None
            and _as_absolute_path(configured, label="PX4_WORKDIR") != selected_px4_cwd
        ):
            raise ExternalStackError("PX4_WORKDIR does not match selected PX4 cwd")
        environment["PX4_WORKDIR"] = str(selected_px4_cwd)
    _validate_environment(
        stack_plan,
        environment,
        px4_workdir=selected_px4_cwd if workdir_ok else None,
    )

    px4_command = _normalise_command(stack_plan.px4_command, label="PX4 command")
    gazebo_command = _normalise_command(stack_plan.gazebo_command, label="Gazebo command")
    _command_executable_matches(px4_command, stack_plan.px4_executable, label="PX4")
    _command_executable_matches(gazebo_command, stack_plan.gazebo_executable, label="Gazebo")
    startup = _startup_script(px4_command)
    if startup is not None and not startup[1] and workdir_reason is not None:
        # Keep the reason in evidence; strict mode has already raised in the
        # resolver.  The command itself remains untouched for reproducibility.
        pass
    world_path = str(stack_plan.world_path.resolve())
    if world_path not in gazebo_command:
        raise ExternalStackError("Gazebo command must reference the resolved repository world")
    if any(_unsafe_endpoint_token(token) for token in (*px4_command, *gazebo_command)):
        raise ExternalStackError("SIM process commands contain a non-local endpoint")

    tools_available = _tool_available(stack_plan, "px4") and _tool_available(stack_plan, "gazebo")
    if strict and not tools_available:
        missing = [name for name in DEFAULT_PROCESS_ORDER if not _tool_available(stack_plan, name)]
        raise ExternalStackError("simulator tools unavailable: " + ", ".join(missing))
    if strict and not workdir_ok:
        raise ExternalStackError(workdir_reason or "PX4_WORKDIR is unresolved")

    px4_launch: Px4LaunchPlan | None = None
    if px4_launch_enabled:
        # Use the same job directory and endpoint/identity source as the
        # external process plan.  Strict wrapper readiness is independently
        # selectable; by default it follows the plan's strict mode.
        launch_strict = strict if px4_launch_strict is None else px4_launch_strict
        try:
            px4_launch = build_px4_launch_plan(
                stack_plan,
                job_dir=selected_job_dir,
                px4_workdir=px4_workdir,
                dry_run=dry_run,
                strict=launch_strict,
            )
        except (Px4LaunchError, OSError, TypeError, ValueError) as exc:
            raise ExternalStackError(f"PX4 launch wrapper: {exc}") from exc

    logs: dict[str, Path] = {
        "px4": selected_log_dir / "px4.log",
        "gazebo": selected_log_dir / "gazebo.log",
    }
    for path in logs.values():
        _validate_log_destination(path, job_dir=selected_job_dir)

    px4_process_command = px4_command
    px4_process_cwd = selected_px4_cwd
    px4_process_environment = dict(environment)
    if px4_launch is not None:
        # The wrapper owns the patched rc.mavlink and must be the first argv
        # token; retain the raw command in source_stack_plan for provenance.
        px4_process_command = tuple(px4_launch.command)
        px4_process_cwd = px4_launch.px4_workdir
        px4_process_environment.update(px4_launch.environment_mapping)

    specs = (
        ExternalProcessSpec(
            name="px4",
            command=px4_process_command,
            cwd=px4_process_cwd,
            env=px4_process_environment,
            log_path=logs["px4"],
            order=0,
            depends_on=(),
        ),
        ExternalProcessSpec(
            name="gazebo",
            command=gazebo_command,
            cwd=gazebo_cwd,
            env=environment,
            log_path=logs["gazebo"],
            order=1,
            depends_on=("px4",),
        ),
    )
    validation = (
        ("stack_plan_hash_present", bool(stack_plan.evidence_hash)),
        ("environment_valid", True),
        ("cwd_valid", all(item.cwd.is_dir() for item in specs)),
        ("process_order_valid", True),
        ("log_paths_valid", True),
        ("loopback_only", True),
        ("tools_available", tools_available),
        ("px4_workdir_resolved", workdir_ok),
        ("px4_launch_ready", True if px4_launch is None else px4_launch.ready_to_launch),
        ("external_processes_started", False),
        ("external_io_opened", False),
    )
    return ExternalStackPlan(
        stack_plan=stack_plan,
        job_dir=selected_job_dir,
        processes=specs,
        dry_run=dry_run,
        process_order=order,
        px4_workdir=selected_px4_cwd if workdir_ok else None,
        px4_workdir_resolved=workdir_ok,
        inherit_environment=inherit_environment,
        log_directory=selected_log_dir,
        validation=validation,
        px4_launch=px4_launch,
    )


class SimulatorExternalStackLauncher:
    """Facade that prepares/evidences a plan and *never* starts processes."""

    def __init__(self, stack_plan: SimulatorStackPlan, **kwargs: Any) -> None:
        self._plan = build_external_stack_plan(stack_plan, **kwargs)

    @property
    def plan(self) -> ExternalStackPlan:
        return self._plan

    def prepare(self) -> ExternalStackPlan:
        return self._plan

    def dry_run(self, path: str | Path | None = None) -> ExternalStackPlan:
        if path is not None:
            self._plan.write_json(path)
        return self._plan

    @property
    def dry_run_evidence(self) -> dict[str, object]:
        return self._plan.to_mapping_with_hash()

    def write_evidence(self, path: str | Path | None = None) -> Path:
        target = (
            _as_absolute_path(path, label="evidence path")
            if path is not None
            else self._plan.job_dir / DEFAULT_EVIDENCE_NAME
        )
        return self._plan.write_json(target)

    # Explicit aliases make the no-launch contract obvious to callers that
    # previously expected an orchestrator-like API.
    start = dry_run
    launch = dry_run
    run = dry_run
    execute = dry_run


# Functional aliases used by scripts and downstream integrations.
ExternalStackLauncher = SimulatorExternalStackLauncher
SimulatorExternalStackPlan = ExternalStackPlan
SimulatorExternalProcessSpec = ExternalProcessSpec
SimulatorExternalStack = ExternalStackPlan
ExternalStackLaunchPlan = ExternalStackPlan
ExternalProcess = ExternalProcessSpec
build_external_launch_plan = build_external_stack_plan
plan_external_stack = build_external_stack_plan
prepare_external_stack = build_external_stack_plan
create_external_stack_plan = build_external_stack_plan
make_external_process_plan = build_external_stack_plan
external_stack_from_plan = build_external_stack_plan


__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_PROCESS_ORDER",
    "DEFAULT_LOG_DIRECTORY_NAME",
    "DEFAULT_EVIDENCE_NAME",
    "ExternalStackError",
    "SimulatorExternalStackError",
    "ExternalStackValidationError",
    "ExternalProcessSpec",
    "SimulatorExternalProcessSpec",
    "ExternalProcess",
    "ExternalStackPlan",
    "ExternalStackLaunchPlan",
    "SimulatorExternalStackPlan",
    "SimulatorExternalStack",
    "SimulatorExternalStackLauncher",
    "ExternalStackLauncher",
    "build_external_stack_plan",
    "build_external_launch_plan",
    "plan_external_stack",
    "prepare_external_stack",
    "create_external_stack_plan",
    "make_external_process_plan",
    "external_stack_from_plan",
]
