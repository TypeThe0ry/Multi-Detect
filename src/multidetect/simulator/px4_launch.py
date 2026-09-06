"""Hash-bound PX4 SITL launch planning for the isolated SIM runtime.

The normal PX4 POSIX startup scripts use a pair of implicit UDP defaults:
``18570`` for the GCS link and ``14580`` for the offboard link.  Those
defaults are not sufficient for the Multi-Detect SIM topology, where the
router owns a deterministic telemetry socket and the shared control adapter
sends to a deterministic command socket.  This module creates a *job-local*
copy of ``px4-rc.mavlink`` and patches only that copy so that the GCS link is
explicitly bound to the plan's control port and publishes to the plan's
telemetry port.

The module is deliberately a planner, not a process runner.  It never edits a
PX4 checkout, starts a process, opens a socket, or imports a truth/evaluator
channel.  A caller can use :class:`Px4LaunchPlan.command` (the generated
wrapper) with its own process supervisor after the readiness contract has
passed.  ``dry_run=True`` remains useful on a clean developer machine: absent
PX4 binaries or startup files are recorded as readiness failures while a
deterministic template and evidence document are still produced.  Strict mode
(``dry_run=False`` or ``strict=True``) turns those failures into an exception.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..autonomy.serialization import canonical_sha256, require_sha256
from .stack import LOOPBACK_HOSTS, SimulatorStackPlan

SCHEMA_VERSION = 1
DEFAULT_JOB_DIRECTORY_NAME = "px4-launch"
DEFAULT_OVERLAY_DIRECTORY_NAME = "px4-init"
DEFAULT_WRAPPER_NAME = "px4-sim-launch.sh"
DEFAULT_PATCHED_RC_NAME = "px4-rc.mavlink"
DEFAULT_GENERATED_RCS_NAME = "rcS"
DEFAULT_RC_S_RELATIVE = Path("etc/init.d-posix/rcS")
DEFAULT_RC_MAVLINK_RELATIVE = Path("etc/init.d-posix/px4-rc.mavlink")

_PORT_MIN = 1024
_PORT_MAX = 65535
_MARKER_BEGIN = "# BEGIN MULTIDETECT SIM MAVLINK OVERRIDE"
_MARKER_END = "# END MULTIDETECT SIM MAVLINK OVERRIDE"
_GCS_START = re.compile(
    r"^(?P<indent>\s*)mavlink\s+start\s+-x\s+-u\s+\$udp_gcs_port_local(?P<rest>.*)$"
)
_PATCHED_GCS_START = re.compile(
    r"^(?P<indent>\s*)mavlink\s+start\s+-x\s+-t\s+[^\s]+\s+-u\s+\d{1,5}"
    r"\s+-o\s+\d{1,5}(?P<rest>.*)$"
)
_GCS_ASSIGNMENT = re.compile(r"^(?P<indent>\s*)udp_gcs_port_local\s*=.*$")
_OPTION_WITH_VALUE = re.compile(
    r"\s+(?:-t|-o)\s+(?:'[^']*'|\"[^\"]*\"|[^\s]+)",
)
# POSIX PX4 ``rcS`` sources the GCS startup script as ``. px4-rc.mavlink``.
# Keep the matcher deliberately narrow: replacing an arbitrary shell command
# in a vendor startup file would make the generated plan impossible to audit.
_RCS_MAVLINK_SOURCE = re.compile(
    r"^(?P<indent>\s*)\.\s+(?:[\"']?)(?:px4-rc\.mavlink)(?:[\"']?)\s*$"
)
_RCS_OVERRIDE_BEGIN = "# BEGIN MULTIDETECT SIM PX4 RCS OVERRIDE"
_RCS_OVERRIDE_END = "# END MULTIDETECT SIM PX4 RCS OVERRIDE"


class Px4LaunchError(RuntimeError):
    """Raised when a PX4 SIM launch plan violates its strict contract."""


# Compatibility spellings used by a few downstream prototype branches.
PX4LaunchError = Px4LaunchError
SimulatorPx4LaunchError = Px4LaunchError
SimulatorPX4LaunchError = Px4LaunchError


def _absolute_path(value: str | Path, *, label: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise Px4LaunchError(f"{label} must be a path")
    raw = str(value).strip()
    if not raw:
        raise Px4LaunchError(f"{label} must not be empty")
    candidate = Path(raw).expanduser()
    # Preserve the symlink check before canonicalization.  ``resolve`` would
    # otherwise hide both live and dangling final symlinks from the containment
    # and job-ownership checks below.
    if candidate.is_symlink():
        raise Px4LaunchError(f"{label} must not be a symlink: {candidate}")
    return candidate.resolve(strict=False)


def _directory(path: Path, *, label: str, create: bool = False) -> Path:
    if path.is_symlink():
        raise Px4LaunchError(f"{label} must not be a symlink: {path}")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise Px4LaunchError(f"{label} is not a directory: {path}")
    return path.resolve()


def _port(value: object, *, label: str) -> int:
    if isinstance(value, bool):
        raise Px4LaunchError(f"{label} must be an integer port")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise Px4LaunchError(f"{label} must be an integer port") from exc
    if not _PORT_MIN <= result <= _PORT_MAX:
        raise Px4LaunchError(f"{label} must be in {_PORT_MIN}..{_PORT_MAX}")
    return result


def _loopback(host: object, *, label: str = "host") -> str:
    if not isinstance(host, str) or host not in LOOPBACK_HOSTS:
        raise Px4LaunchError(f"{label} must be literal loopback (127.0.0.1 or ::1)")
    return host


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_atomic(path: Path, data: bytes, *, overwrite: bool = True) -> None:
    """Write one job-owned file without touching any source checkout file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise Px4LaunchError(f"generated artifact must not be a symlink: {path}")
    if path.exists() and not overwrite:
        raise Px4LaunchError(f"generated artifact already exists: {path}")
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def _shell_quote(value: str | Path) -> str:
    """Quote a path for the POSIX shell used by PX4 SITL wrappers.

    PX4 SITL is normally run in Linux (locally or in a container), while the
    planner itself is also used from Windows.  ``shlex.quote`` keeps the
    resulting wrapper syntactically safe in both cases; a Windows path is
    intentionally retained verbatim in evidence and can be translated by a
    container launcher that mounts the job directory.
    """

    return shlex.quote(str(value))


def _strip_existing_marker(lines: list[str]) -> list[str]:
    result: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == _MARKER_BEGIN:
            inside = True
            continue
        if inside and line.strip() == _MARKER_END:
            inside = False
            continue
        if not inside:
            result.append(line)
    return result


def patch_rc_mavlink(
    source: str,
    *,
    control_port: int,
    telemetry_port: int,
    target_host: str = "127.0.0.1",
    stack_plan_sha256: str,
    scenario_manifest_sha256: str,
) -> str:
    """Return a job-local ``px4-rc.mavlink`` with explicit SIM ports.

    The stock POSIX script contains a stable GCS stanza beginning with
    ``mavlink start -x -u $udp_gcs_port_local``.  We replace that one command,
    retain its rate/interface arguments, and set ``udp_gcs_port_local`` to the
    plan's control port so all subsequent ``mavlink stream`` commands use the
    same socket.  Existing ``-t``/``-o`` values on the selected line are
    removed before adding the bound values, making the operation idempotent.

    ``source`` is a pure string input; no file is edited.  A malformed source
    (without a GCS stanza) raises so the caller can mark strict readiness as
    failed.  The builder uses a separate template when the source is absent in
    dry-run mode.
    """

    control = _port(control_port, label="control_port")
    telemetry = _port(telemetry_port, label="telemetry_port")
    if control == telemetry:
        raise Px4LaunchError("control_port and telemetry_port must be distinct")
    host = _loopback(target_host, label="target_host")
    plan_hash = require_sha256(stack_plan_sha256, "stack_plan_sha256")
    scenario_hash = require_sha256(scenario_manifest_sha256, "scenario_manifest_sha256")
    if not isinstance(source, str):
        raise Px4LaunchError("rc.mavlink source must be text")

    # Normalize CRLF from Windows checkouts.  PX4's shell parser must not see
    # carriage returns in the generated script.
    marker_present = _MARKER_BEGIN in source
    lines = _strip_existing_marker(source.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    # ``split`` leaves one terminal empty line; remove it and restore exactly
    # one newline at the end below.
    while lines and lines[-1] == "":
        lines.pop()

    gcs_index: int | None = None
    assignment_index: int | None = None
    for index, line in enumerate(lines):
        if assignment_index is None and _GCS_ASSIGNMENT.match(line):
            assignment_index = index
        if gcs_index is None and (
            _GCS_START.match(line) or (marker_present and _PATCHED_GCS_START.match(line))
        ):
            gcs_index = index

    if gcs_index is None:
        raise Px4LaunchError(
            "PX4 rc.mavlink does not contain the expected GCS mavlink start stanza"
        )

    if assignment_index is not None:
        indent = _GCS_ASSIGNMENT.match(lines[assignment_index]).group("indent")  # type: ignore[union-attr]
        lines[assignment_index] = f"{indent}udp_gcs_port_local={control}"
    else:
        # The selected start line can still be made explicit even if a future
        # PX4 script computes the variable in a helper.
        lines.insert(gcs_index, f"udp_gcs_port_local={control}")
        gcs_index += 1

    match = _GCS_START.match(lines[gcs_index])
    if match is None and marker_present:
        match = _PATCHED_GCS_START.match(lines[gcs_index])
    if match is None:  # defensive: insertion/index drift should never happen
        raise Px4LaunchError("unable to patch PX4 GCS mavlink start stanza")
    rest = _OPTION_WITH_VALUE.sub("", match.group("rest"))
    # Keep the original indentation and all non-endpoint arguments (`-r`,
    # `-f`, interface mode, etc.) exactly as supplied by PX4.
    lines[gcs_index] = (
        f"{match.group('indent')}mavlink start -x -t {host} -u {control} -o {telemetry}{rest}"
    ).rstrip()

    marker = [
        _MARKER_BEGIN,
        f"# MD_SIM_STACK_PLAN_SHA256={plan_hash}",
        f"# MD_SIM_SCENARIO_SHA256={scenario_hash}",
        f"# MD_SIM_CONTROL_PORT={control}",
        f"# MD_SIM_TELEMETRY_PORT={telemetry}",
        f"# MD_SIM_TARGET_HOST={host}",
        _MARKER_END,
    ]
    # Keep the shebang as line one, then put the provenance marker immediately
    # before the first executable statement.
    insert_at = 1 if lines and lines[0].startswith("#!") else 0
    lines[insert_at:insert_at] = marker
    return "\n".join(lines).rstrip("\n") + "\n"


def patch_rcS(
    source: str,
    *,
    patched_rc_mavlink: str | Path,
    stack_plan_sha256: str,
    scenario_manifest_sha256: str,
) -> str:
    """Return a job-local ``rcS`` which explicitly sources the patched file.

    PX4's POSIX ``rcS`` normally resolves ``px4-rc.mavlink`` through ``PATH``.
    That is useful for normal builds, but it leaves a launch plan dependent on
    ambient path ordering.  This helper keeps the original script intact and
    replaces only its exact source statement with an absolute, hash-bound path
    to the job-local copy.  A provenance block makes the operation idempotent
    and lets :meth:`Px4LaunchPlan.verify` prove which file is selected at run
    time.
    """

    if not isinstance(source, str):
        raise Px4LaunchError("rcS source must be text")
    plan_hash = require_sha256(stack_plan_sha256, "stack_plan_sha256")
    scenario_hash = require_sha256(scenario_manifest_sha256, "scenario_manifest_sha256")
    raw_path = str(patched_rc_mavlink).strip()
    if not raw_path:
        raise Px4LaunchError("patched_rc_mavlink must not be empty")
    quoted_path = _shell_quote(raw_path)

    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    while lines and lines[-1] == "":
        lines.pop()

    # Remove a previous generated block while retaining its insertion index.
    # If the source was generated by an older planner without a block, the
    # exact ``. px4-rc.mavlink`` line below remains the replacement anchor.
    block_start: int | None = None
    block_end: int | None = None
    for index, line in enumerate(lines):
        if line.strip() == _RCS_OVERRIDE_BEGIN:
            block_start = index
            for end_index in range(index + 1, len(lines)):
                if lines[end_index].strip() == _RCS_OVERRIDE_END:
                    block_end = end_index
                    break
            if block_end is None:
                raise Px4LaunchError("rcS contains an unterminated Multi-Detect override block")
            break

    if block_start is not None and block_end is not None:
        indent = ""
        # Preserve the indentation of the old source statement when possible.
        for candidate in lines[block_start + 1 : block_end]:
            if candidate.strip().startswith("."):
                indent = candidate[: len(candidate) - len(candidate.lstrip())]
                break
        insertion_at = block_start
        del lines[block_start : block_end + 1]
    else:
        insertion_at = None
        indent = ""
        for index, line in enumerate(lines):
            match = _RCS_MAVLINK_SOURCE.match(line)
            if match is not None:
                insertion_at = index
                indent = match.group("indent")
                break
        if insertion_at is None:
            raise Px4LaunchError(
                "PX4 rcS does not contain the expected `. px4-rc.mavlink` source statement"
            )
        # Replace (rather than prepend to) the original source statement.  A
        # duplicate would execute the stock 18570/14580 stanza after the
        # patched one and silently undo the endpoint isolation contract.
        del lines[insertion_at]

    block = [
        _RCS_OVERRIDE_BEGIN,
        f"# MD_SIM_STACK_PLAN_SHA256={plan_hash}",
        f"# MD_SIM_SCENARIO_SHA256={scenario_hash}",
        f"{indent}MD_SIM_PX4_RC_MAVLINK={quoted_path}",
        f"{indent}export MD_SIM_PX4_RC_MAVLINK",
        f'{indent}. "$MD_SIM_PX4_RC_MAVLINK"',
        _RCS_OVERRIDE_END,
    ]
    assert insertion_at is not None
    lines[insertion_at:insertion_at] = block
    return "\n".join(lines).rstrip("\n") + "\n"


def _template_rcS(
    *,
    patched_rc_mavlink: str | Path,
    stack_plan_sha256: str,
    scenario_manifest_sha256: str,
) -> str:
    """Create a deterministic local ``rcS`` diagnostic template."""

    plan_hash = require_sha256(stack_plan_sha256, "stack_plan_sha256")
    scenario_hash = require_sha256(scenario_manifest_sha256, "scenario_manifest_sha256")
    raw_path = str(patched_rc_mavlink).strip()
    if not raw_path:
        raise Px4LaunchError("patched_rc_mavlink must not be empty")
    quoted_path = _shell_quote(raw_path)
    return "\n".join(
        (
            "#!/bin/sh",
            _RCS_OVERRIDE_BEGIN,
            f"# MD_SIM_STACK_PLAN_SHA256={plan_hash}",
            f"# MD_SIM_SCENARIO_SHA256={scenario_hash}",
            f"MD_SIM_PX4_RC_MAVLINK={quoted_path}",
            "export MD_SIM_PX4_RC_MAVLINK",
            '. "$MD_SIM_PX4_RC_MAVLINK"',
            _RCS_OVERRIDE_END,
            "",
        )
    )


def _template_rc_mavlink(
    *,
    control_port: int,
    telemetry_port: int,
    target_host: str,
    stack_plan_sha256: str,
    scenario_manifest_sha256: str,
) -> str:
    """Create a deterministic dry-run template when PX4 is not installed."""

    plan_hash = require_sha256(stack_plan_sha256, "stack_plan_sha256")
    scenario_hash = require_sha256(scenario_manifest_sha256, "scenario_manifest_sha256")
    control = _port(control_port, label="control_port")
    telemetry = _port(telemetry_port, label="telemetry_port")
    host = _loopback(target_host, label="target_host")
    return "\n".join(
        (
            "#!/bin/sh",
            _MARKER_BEGIN,
            f"# MD_SIM_STACK_PLAN_SHA256={plan_hash}",
            f"# MD_SIM_SCENARIO_SHA256={scenario_hash}",
            f"# MD_SIM_CONTROL_PORT={control}",
            f"# MD_SIM_TELEMETRY_PORT={telemetry}",
            f"# MD_SIM_TARGET_HOST={host}",
            _MARKER_END,
            f"udp_gcs_port_local={control}",
            f"mavlink start -x -t {host} -u {control} -o {telemetry}",
            f"mavlink stream -r 50 -s GLOBAL_POSITION_INT -u {control}",
            "",
        )
    )


def _wrapper_text(
    *,
    px4_executable: str,
    px4_workdir: Path,
    overlay_dir: Path,
    startup_script: Path | None = None,
    patched_rc_mavlink: Path | None = None,
    control_port: int,
    telemetry_port: int,
    target_host: str,
    stack_plan_sha256: str,
    scenario_manifest_sha256: str,
) -> str:
    plan_hash = require_sha256(stack_plan_sha256, "stack_plan_sha256")
    scenario_hash = require_sha256(scenario_manifest_sha256, "scenario_manifest_sha256")
    control = _port(control_port, label="control_port")
    telemetry = _port(telemetry_port, label="telemetry_port")
    host = _loopback(target_host, label="target_host")
    workdir = _shell_quote(px4_workdir)
    overlay = _shell_quote(overlay_dir)
    executable = _shell_quote(px4_executable)
    # ``startup_script`` is the job-local rcS that explicitly sources the
    # patched MAVLink script.  Keep the historical relative path as a comment
    # for downstream log parsers, but never use it as the active command.
    startup = _shell_quote(startup_script or Path("etc/init.d-posix/rcS"))
    patched = _shell_quote(patched_rc_mavlink or (overlay_dir / "px4-rc.mavlink"))
    return "\n".join(
        (
            "#!/bin/sh",
            "set -eu",
            f"{_MARKER_BEGIN}",
            f"# MD_SIM_STACK_PLAN_SHA256={plan_hash}",
            f"# MD_SIM_SCENARIO_SHA256={scenario_hash}",
            f"# MD_SIM_CONTROL_PORT={control}",
            f"# MD_SIM_TELEMETRY_PORT={telemetry}",
            f"# MD_SIM_TARGET_HOST={host}",
            _MARKER_END,
            f"MD_SIM_PX4_WORKDIR={workdir}",
            f"MD_SIM_PX4_OVERLAY={overlay}",
            f"MD_SIM_PX4_RCS={startup}",
            f"MD_SIM_PX4_RC_MAVLINK={patched}",
            'cd "$MD_SIM_PX4_WORKDIR"',
            'export PATH="$MD_SIM_PX4_OVERLAY:$PATH"',
            "export MD_SIM_PX4_RC_MAVLINK",
            # Explicitly prove the same file that the generated rcS sources is
            # first on PATH.  The check catches a stale overlay before PX4 can
            # start with its stock 18570/14580 endpoints.
            'test -f "$MD_SIM_PX4_RCS"',
            'test -f "$MD_SIM_PX4_RC_MAVLINK"',
            'test "$(command -v px4-rc.mavlink)" = "$MD_SIM_PX4_RC_MAVLINK"',
            # PX4's stock rc.mavlink turns PX4_NET_INTERFACE into ``-n`` and
            # ``-p`` (interface bind/broadcast).  Clear any inherited host
            # value so this SIM wrapper cannot silently leave the literal
            # loopback topology.
            "export PX4_NET_INTERFACE=",
            f"export PX4_MAVLINK_CONTROL_PORT={control}",
            f"export PX4_MAVLINK_UDP_PORT={control}",
            f"export PX4_MAVLINK_TELEMETRY_PORT={telemetry}",
            f"export PX4_MAVLINK_TARGET_HOST={host}",
            "# Legacy relative startup reference (not executed): -i 0 -s etc/init.d-posix/rcS",
            f'exec {executable} -i 0 -s "$MD_SIM_PX4_RCS" "$@"',
            "",
        )
    )


def _resolve_executable(plan: SimulatorStackPlan) -> tuple[str, Path | None, bool]:
    raw = str(plan.px4_executable).strip()
    if not raw:
        raise Px4LaunchError("stack plan PX4 executable is empty")
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=False)
        return str(resolved), resolved, resolved.is_file()
    found = shutil.which(raw)
    return raw, None if found is None else Path(found).resolve(), found is not None


def _resolve_workdir(
    plan: SimulatorStackPlan,
    *,
    requested: str | Path | None,
) -> tuple[Path | None, bool]:
    environment = plan.environment_mapping
    raw = requested if requested is not None else environment.get("PX4_WORKDIR")
    candidate: Path | None = None
    if raw is not None:
        candidate = _absolute_path(raw, label="PX4_WORKDIR")
    else:
        executable = Path(str(plan.px4_executable)).expanduser()
        if not executable.is_absolute():
            resolved = shutil.which(str(executable))
            if resolved:
                executable = Path(resolved)
        if executable.is_absolute() and executable.is_file():
            candidate = (
                executable.parent.parent
                if executable.parent.name.lower() == "bin"
                else executable.parent
            )
    if candidate is None:
        return None, False
    if candidate.is_symlink():
        raise Px4LaunchError(f"PX4_WORKDIR must not be a symlink: {candidate}")
    return candidate.resolve(strict=False), candidate.is_dir()


def _check_file(path: Path | None) -> bool:
    return path is not None and path.is_file()


@dataclass(frozen=True, slots=True)
class Px4LaunchPlan:
    """One job-owned PX4 wrapper and patched startup-script plan."""

    stack_plan: SimulatorStackPlan
    job_dir: Path
    px4_executable: str
    px4_executable_path: Path | None
    px4_executable_available: bool
    px4_workdir: Path
    px4_workdir_resolved: bool
    rcS_path: Path | None
    source_rc_mavlink: Path | None
    generated_rc_mavlink: Path
    wrapper_path: Path
    command: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    control_port: int
    telemetry_port: int
    target_host: str
    source_rc_mavlink_sha256: str | None
    source_rcS_sha256: str | None
    generated_rc_mavlink_sha256: str
    wrapper_sha256: str
    dry_run: bool
    strict: bool
    source_is_template: bool
    startup_patch_applied: bool
    readiness: tuple[tuple[str, bool], ...]
    probe_commands: tuple[str, ...]
    schema_version: int = SCHEMA_VERSION
    # A job-local copy of ``rcS`` is generated alongside the patched
    # ``px4-rc.mavlink``.  Optional defaults preserve source compatibility for
    # callers that constructed plans before explicit rcS binding was added.
    generated_rcS: Path | None = None
    generated_rcS_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise Px4LaunchError(f"unsupported PX4 launch schema {self.schema_version}")
        if not isinstance(self.stack_plan, SimulatorStackPlan):
            raise TypeError("stack_plan must be SimulatorStackPlan")
        object.__setattr__(self, "job_dir", _absolute_path(self.job_dir, label="job_dir"))
        object.__setattr__(
            self,
            "generated_rc_mavlink",
            _absolute_path(self.generated_rc_mavlink, label="generated_rc_mavlink"),
        )
        object.__setattr__(
            self,
            "wrapper_path",
            _absolute_path(self.wrapper_path, label="wrapper_path"),
        )
        for artifact, label in (
            (self.generated_rc_mavlink, "generated_rc_mavlink"),
            (self.wrapper_path, "wrapper_path"),
        ):
            try:
                artifact.relative_to(self.job_dir)
            except ValueError as exc:
                raise Px4LaunchError(f"{label} must remain inside job_dir") from exc
            if artifact.is_symlink():
                raise Px4LaunchError(f"{label} must not be a symlink: {artifact}")
        if self.generated_rcS is not None:
            object.__setattr__(
                self,
                "generated_rcS",
                _absolute_path(self.generated_rcS, label="generated_rcS"),
            )
            try:
                self.generated_rcS.relative_to(self.job_dir)
            except ValueError as exc:
                raise Px4LaunchError("generated_rcS must remain inside job_dir") from exc
            if self.generated_rcS.is_symlink():
                raise Px4LaunchError(f"generated_rcS must not be a symlink: {self.generated_rcS}")
            if self.generated_rcS_sha256 is not None:
                require_sha256(self.generated_rcS_sha256, "generated_rcS_sha256")
        require_sha256(self.stack_plan.evidence_hash, "stack_plan_sha256")
        if self.control_port == self.telemetry_port:
            raise Px4LaunchError("control_port and telemetry_port must be distinct")
        _port(self.control_port, label="control_port")
        _port(self.telemetry_port, label="telemetry_port")
        _loopback(self.target_host, label="target_host")
        if tuple(sorted(self.readiness)) != self.readiness:
            raise Px4LaunchError("readiness entries must be sorted")

    @property
    def source_stack_plan_sha256(self) -> str:
        return self.stack_plan.evidence_hash

    @property
    def scenario_manifest_sha256(self) -> str:
        return self.stack_plan.scenario_manifest_hash

    @property
    def px4_command(self) -> tuple[str, ...]:
        """Alias used by process supervisors."""

        return self.command

    @property
    def cwd(self) -> Path:
        return self.px4_workdir

    @property
    def environment_mapping(self) -> dict[str, str]:
        return dict(self.environment)

    @property
    def readiness_mapping(self) -> dict[str, bool]:
        return dict(self.readiness)

    @property
    def ready_to_launch(self) -> bool:
        return all(self.readiness_mapping.values())

    @property
    def evidence_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @property
    def evidence(self) -> dict[str, object]:
        return self.to_mapping_with_hash()

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event": "simulator_px4_launch_plan",
            "scenario_id": self.stack_plan.scenario_id,
            "scenario_version": self.stack_plan.scenario_version,
            "scenario_manifest_sha256": self.scenario_manifest_sha256,
            "source_stack_plan_sha256": self.source_stack_plan_sha256,
            "job_dir": str(self.job_dir),
            "dry_run": self.dry_run,
            "strict": self.strict,
            "px4": {
                "executable": self.px4_executable,
                "resolved_executable": None
                if self.px4_executable_path is None
                else str(self.px4_executable_path),
                "available": self.px4_executable_available,
                "workdir": str(self.px4_workdir),
                "workdir_resolved": self.px4_workdir_resolved,
                "rcS": None if self.rcS_path is None else str(self.rcS_path),
                "generated_rcS": None if self.generated_rcS is None else str(self.generated_rcS),
                "rc_mavlink_source": None
                if self.source_rc_mavlink is None
                else str(self.source_rc_mavlink),
                "command": list(self.command),
                "cwd": str(self.cwd),
            },
            "ports": {
                "host": self.target_host,
                "control_port": self.control_port,
                "telemetry_port": self.telemetry_port,
                "control_endpoint": f"udpout:{self.target_host}:{self.control_port}",
                "telemetry_endpoint": f"udp://{self.target_host}:{self.telemetry_port}",
                "loopback_only": self.target_host in LOOPBACK_HOSTS,
                "distinct": self.control_port != self.telemetry_port,
            },
            "environment": self.environment_mapping,
            "artifacts": {
                "generated_rc_mavlink": str(self.generated_rc_mavlink),
                "generated_rc_mavlink_sha256": self.generated_rc_mavlink_sha256,
                "generated_rcS": None if self.generated_rcS is None else str(self.generated_rcS),
                "generated_rcS_sha256": self.generated_rcS_sha256,
                "wrapper": str(self.wrapper_path),
                "wrapper_sha256": self.wrapper_sha256,
                "source_rc_mavlink": None
                if self.source_rc_mavlink is None
                else str(self.source_rc_mavlink),
                "source_rc_mavlink_sha256": self.source_rc_mavlink_sha256,
                "source_rcS_sha256": self.source_rcS_sha256,
                "source_is_template": self.source_is_template,
                "startup_patch_applied": self.startup_patch_applied,
                "source_checkout_modified": False,
            },
            "readiness": {
                "checks": self.readiness_mapping,
                "gate_passed": self.ready_to_launch,
                "required": [key for key, _ in self.readiness],
                "probe_commands": list(self.probe_commands),
                "process_started": False,
                "external_io_opened": False,
                "telemetry_observed": False,
            },
            "runtime": {
                "external_process_started": False,
                "external_io_opened": False,
                "source_checkout_modified": False,
                "truth_free": True,
            },
            "truth_free": True,
        }

    def to_mapping_with_hash(self) -> dict[str, object]:
        result = self.to_mapping()
        result["sha256"] = self.evidence_hash
        return result

    def as_dict(self) -> dict[str, object]:
        return self.to_mapping_with_hash()

    def verify(self) -> bool:
        """Re-hash job artifacts and verify provenance markers."""

        if not self.generated_rc_mavlink.is_file() or not self.wrapper_path.is_file():
            return False
        if _sha256_file(self.generated_rc_mavlink) != self.generated_rc_mavlink_sha256:
            return False
        if _sha256_file(self.wrapper_path) != self.wrapper_sha256:
            return False
        text = self.generated_rc_mavlink.read_text(encoding="utf-8")
        wrapper = self.wrapper_path.read_text(encoding="utf-8")
        marker = f"MD_SIM_STACK_PLAN_SHA256={self.source_stack_plan_sha256}"
        if marker not in text or marker not in wrapper:
            return False
        # Plans emitted before explicit rcS binding may have these fields set
        # to ``None``.  Preserve verification compatibility for those objects,
        # while requiring the stronger checks for every newly-built plan.
        if self.generated_rcS is None or self.generated_rcS_sha256 is None:
            return True
        if not self.generated_rcS.is_file():
            return False
        if _sha256_file(self.generated_rcS) != self.generated_rcS_sha256:
            return False
        rcS = self.generated_rcS.read_text(encoding="utf-8")
        explicit_marker = f"MD_SIM_SCENARIO_SHA256={self.scenario_manifest_sha256}"
        expected_source = '. "$MD_SIM_PX4_RC_MAVLINK"'
        return (
            _RCS_OVERRIDE_BEGIN in rcS
            and _RCS_OVERRIDE_END in rcS
            and marker in rcS
            and explicit_marker in rcS
            and expected_source in rcS
            and "MD_SIM_PX4_RCS=" in wrapper
            and '-s "$MD_SIM_PX4_RCS"' in wrapper
            and "command -v px4-rc.mavlink" in wrapper
        )

    def write_evidence(self, path: str | Path | None = None, *, overwrite: bool = True) -> Path:
        target = (
            _absolute_path(path, label="evidence path")
            if path is not None
            else self.job_dir / "px4-launch.json"
        )
        try:
            target.relative_to(self.job_dir)
        except ValueError as exc:
            raise Px4LaunchError("evidence path must remain inside job_dir") from exc
        _write_atomic(
            target,
            (
                json.dumps(
                    self.to_mapping_with_hash(),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8"),
            overwrite=overwrite,
        )
        return target


def build_px4_launch_plan(
    stack_plan: SimulatorStackPlan,
    *,
    job_dir: str | Path | None = None,
    px4_workdir: str | Path | None = None,
    control_port: int | None = None,
    telemetry_port: int | None = None,
    target_host: str | None = None,
    dry_run: bool = True,
    strict: bool | None = None,
    overwrite: bool = True,
    write_evidence: bool = True,
) -> Px4LaunchPlan:
    """Prepare a job-local PX4 startup wrapper without launching PX4.

    ``stack_plan`` is the sole source of truth for scenario identity and
    endpoints.  Optional port arguments are accepted only when they match the
    plan, preventing a wrapper/evidence split-brain.  In dry-run mode absent
    binaries and startup files become failed readiness checks; strict mode
    raises after writing the diagnostic job artifacts.
    """

    if not isinstance(stack_plan, SimulatorStackPlan):
        raise TypeError("stack_plan must be SimulatorStackPlan")
    if not isinstance(dry_run, bool):
        raise TypeError("dry_run must be bool")
    if strict is None:
        strict = not dry_run
    if not isinstance(strict, bool):
        raise TypeError("strict must be bool or None")
    plan_control = _port(stack_plan.ports.px4_control_port, label="stack plan control port")
    plan_telemetry = _port(stack_plan.ports.px4_telemetry_port, label="stack plan telemetry port")
    control = plan_control if control_port is None else _port(control_port, label="control_port")
    telemetry = (
        plan_telemetry if telemetry_port is None else _port(telemetry_port, label="telemetry_port")
    )
    if control != plan_control:
        raise Px4LaunchError(
            f"control_port conflicts with stack plan: expected {plan_control}, got {control}"
        )
    if telemetry != plan_telemetry:
        raise Px4LaunchError(
            f"telemetry_port conflicts with stack plan: expected {plan_telemetry}, got {telemetry}"
        )
    if control == telemetry:
        raise Px4LaunchError("control_port and telemetry_port must be distinct")
    host = _loopback(stack_plan.ports.host if target_host is None else target_host)
    if host != stack_plan.ports.host:
        raise Px4LaunchError(
            f"target_host conflicts with stack plan: expected {stack_plan.ports.host}, got {host}"
        )

    if job_dir is None:
        job = (
            stack_plan.repository_root
            / "artifacts"
            / "evaluation"
            / (
                f"{DEFAULT_JOB_DIRECTORY_NAME}-{stack_plan.scenario_id}-{stack_plan.evidence_hash[:12]}"
            )
        )
    else:
        job = _absolute_path(job_dir, label="job_dir")
    job = _directory(job, label="job_dir", create=True)
    overlay = _directory(job / DEFAULT_OVERLAY_DIRECTORY_NAME, label="PX4 overlay", create=True)

    executable, executable_path, executable_available = _resolve_executable(stack_plan)
    workdir, workdir_resolved = _resolve_workdir(stack_plan, requested=px4_workdir)
    if workdir is None:
        # A dry-run wrapper still has a concrete cwd.  It is never reported as
        # resolved and strict mode fails below.
        workdir = job
    if workdir_resolved:
        workdir = _directory(workdir, label="PX4_WORKDIR")
    elif strict and px4_workdir is not None:
        # An explicitly supplied invalid directory is a hard error even when
        # the caller asked for a strict plan; dry-run retains the failed check.
        raise Px4LaunchError(f"PX4_WORKDIR is not a directory: {workdir}")

    rcS = None if not workdir_resolved else workdir / DEFAULT_RC_S_RELATIVE
    source_rc = None if not workdir_resolved else workdir / DEFAULT_RC_MAVLINK_RELATIVE
    rcS_exists = _check_file(rcS)
    source_exists = _check_file(source_rc)
    source_hash: str | None = None
    rcS_hash: str | None = None
    startup_patch_applied = False
    startup_rcS_patch_applied = False
    source_is_template = False
    # Both generated scripts live below the disposable job directory.  The
    # rcS copy is created before patching so its absolute source path can be
    # embedded directly in the replacement block.
    generated_rc = overlay / DEFAULT_PATCHED_RC_NAME
    generated_rcS = overlay / DEFAULT_GENERATED_RCS_NAME
    if source_exists:
        assert source_rc is not None
        raw = source_rc.read_text(encoding="utf-8")
        source_hash = _sha256_file(source_rc)
        try:
            patched = patch_rc_mavlink(
                raw,
                control_port=control,
                telemetry_port=telemetry,
                target_host=host,
                stack_plan_sha256=stack_plan.evidence_hash,
                scenario_manifest_sha256=stack_plan.scenario_manifest_hash,
            )
            startup_patch_applied = True
        except Px4LaunchError:
            # Keep a generated template as a diagnostic artifact.  Strict mode
            # is rejected by the readiness gate below, while dry-run callers
            # still get a complete, hash-bound job directory.
            source_is_template = True
            patched = _template_rc_mavlink(
                control_port=control,
                telemetry_port=telemetry,
                target_host=host,
                stack_plan_sha256=stack_plan.evidence_hash,
                scenario_manifest_sha256=stack_plan.scenario_manifest_hash,
            )
    else:
        source_is_template = True
        patched = _template_rc_mavlink(
            control_port=control,
            telemetry_port=telemetry,
            target_host=host,
            stack_plan_sha256=stack_plan.evidence_hash,
            scenario_manifest_sha256=stack_plan.scenario_manifest_hash,
        )
    if rcS_exists:
        assert rcS is not None
        rcS_hash = _sha256_file(rcS)
        try:
            patched_rcS = patch_rcS(
                rcS.read_text(encoding="utf-8"),
                patched_rc_mavlink=generated_rc,
                stack_plan_sha256=stack_plan.evidence_hash,
                scenario_manifest_sha256=stack_plan.scenario_manifest_hash,
            )
            startup_rcS_patch_applied = True
        except Px4LaunchError:
            # As with the MAVLink script, preserve a deterministic template for
            # dry-run diagnostics but make strict readiness fail explicitly.
            patched_rcS = _template_rcS(
                patched_rc_mavlink=generated_rc,
                stack_plan_sha256=stack_plan.evidence_hash,
                scenario_manifest_sha256=stack_plan.scenario_manifest_hash,
            )
    else:
        patched_rcS = _template_rcS(
            patched_rc_mavlink=generated_rc,
            stack_plan_sha256=stack_plan.evidence_hash,
            scenario_manifest_sha256=stack_plan.scenario_manifest_hash,
        )

    wrapper = job / DEFAULT_WRAPPER_NAME
    _write_atomic(generated_rc, patched.encode("utf-8"), overwrite=overwrite)
    _write_atomic(generated_rcS, patched_rcS.encode("utf-8"), overwrite=overwrite)
    # PX4 sources the files as shell scripts.  Mark both executable so the
    # wrapper's command-resolution assertion is meaningful on POSIX runners;
    # chmod is advisory on Windows and is intentionally ignored there.
    for script in (generated_rc, generated_rcS):
        try:
            script.chmod(script.stat().st_mode | 0o111)
        except OSError:
            pass
    wrapper_text = _wrapper_text(
        px4_executable=executable,
        px4_workdir=workdir,
        overlay_dir=overlay,
        startup_script=generated_rcS,
        patched_rc_mavlink=generated_rc,
        control_port=control,
        telemetry_port=telemetry,
        target_host=host,
        stack_plan_sha256=stack_plan.evidence_hash,
        scenario_manifest_sha256=stack_plan.scenario_manifest_hash,
    )
    _write_atomic(wrapper, wrapper_text.encode("utf-8"), overwrite=overwrite)
    try:
        wrapper.chmod(wrapper.stat().st_mode | 0o111)
    except OSError:
        # chmod is advisory on Windows; the shell wrapper remains represented
        # in evidence and can be made executable after copying into Linux.
        pass

    environment = dict(stack_plan.environment_mapping)
    # The stack plan is immutable-by-contract.  If a caller hand-constructs a
    # plan with stale endpoint environment values, fail before producing a
    # wrapper that disagrees with its evidence.
    for key, expected in (
        ("PX4_MAVLINK_UDP_PORT", control),
        ("PX4_MAVLINK_TELEMETRY_PORT", telemetry),
    ):
        observed = environment.get(key)
        if observed is not None and str(observed) != str(expected):
            raise Px4LaunchError(
                f"{key} conflicts with stack plan: expected {expected}, got {observed}"
            )
    observed_host = environment.get("PX4_SIM_HOST")
    if observed_host is not None and observed_host not in LOOPBACK_HOSTS:
        raise Px4LaunchError("PX4_SIM_HOST must be literal loopback")
    environment.update(
        {
            "PX4_MAVLINK_CONTROL_PORT": str(control),
            "PX4_MAVLINK_UDP_PORT": str(control),
            "PX4_MAVLINK_TELEMETRY_PORT": str(telemetry),
            "PX4_MAVLINK_TARGET_HOST": host,
            "MD_SIM_STACK_PLAN_SHA256": stack_plan.evidence_hash,
            # Keep the scenario digest explicit in the job environment as
            # well as in the stack-plan object.  The wrapper and readiness
            # contract use this value to prove that the generated rc.mavlink
            # belongs to the exact scenario that was planned.
            "MD_SIM_SCENARIO_SHA256": stack_plan.scenario_manifest_hash,
            "MD_SIM_PX4_RC_MAVLINK": str(generated_rc),
            "MD_SIM_PX4_RCS": str(generated_rcS),
            "MD_SIM_PX4_WRAPPER": str(wrapper),
        }
    )
    if workdir_resolved:
        environment["PX4_WORKDIR"] = str(workdir)

    readiness = {
        "stack_plan_hash_present": bool(stack_plan.evidence_hash),
        "scenario_hash_bound": stack_plan.scenario_manifest_hash
        == environment.get("MD_SIM_SCENARIO_SHA256"),
        "loopback_only": host in LOOPBACK_HOSTS,
        "ports_distinct": control != telemetry,
        "control_port_explicit": f"-u {control}" in patched,
        "telemetry_port_explicit": f"-o {telemetry}" in patched,
        "px4_executable_available": executable_available,
        "px4_workdir_resolved": workdir_resolved,
        "rcS_present": rcS_exists,
        "rc_mavlink_source_present": source_exists,
        "startup_patch_applied": startup_patch_applied,
        "generated_rcS_present": generated_rcS.is_file(),
        "startup_rcS_patch_applied": startup_rcS_patch_applied,
        "explicit_rc_mavlink_reference": startup_rcS_patch_applied
        and '. "$MD_SIM_PX4_RC_MAVLINK"' in patched_rcS,
        "generated_rc_mavlink_present": generated_rc.is_file(),
        "generated_rcS_hash_bound": (
            f"MD_SIM_STACK_PLAN_SHA256={stack_plan.evidence_hash}" in patched_rcS
            and f"MD_SIM_SCENARIO_SHA256={stack_plan.scenario_manifest_hash}" in patched_rcS
        ),
        "wrapper_present": wrapper.is_file(),
        "wrapper_hash_bound": (
            f"MD_SIM_STACK_PLAN_SHA256={stack_plan.evidence_hash}" in wrapper_text
        ),
        "source_checkout_untouched": True,
    }
    # Canonical ordering makes evidence hashes stable across Python versions.
    readiness_tuple = tuple(sorted((str(key), bool(value)) for key, value in readiness.items()))
    probe_commands = (
        f"test -x {_shell_quote(wrapper)}",
        f"test -f {_shell_quote(generated_rcS)}  # explicit job-local rcS",
        f"test -f {_shell_quote(rcS or (workdir / DEFAULT_RC_S_RELATIVE))}  # source rcS",
        f"test -f {_shell_quote(generated_rc)}",
        f"ss -H -lun | grep -E ':{control}\\b'  # PX4 control listener",
        f"timeout 3 sh -c \"ss -H -lun | grep -E ':{telemetry}\\\\b'\"  # telemetry endpoint",
        "mavlink status  # run inside PX4 shell after process startup",
    )
    launch_plan = Px4LaunchPlan(
        stack_plan=stack_plan,
        job_dir=job,
        px4_executable=executable,
        px4_executable_path=executable_path,
        px4_executable_available=executable_available,
        px4_workdir=workdir,
        px4_workdir_resolved=workdir_resolved,
        rcS_path=rcS,
        source_rc_mavlink=source_rc,
        generated_rc_mavlink=generated_rc,
        wrapper_path=wrapper,
        command=(str(wrapper),),
        environment=tuple(sorted((str(key), str(value)) for key, value in environment.items())),
        control_port=control,
        telemetry_port=telemetry,
        target_host=host,
        source_rc_mavlink_sha256=source_hash,
        source_rcS_sha256=rcS_hash,
        generated_rc_mavlink_sha256=_sha256_file(generated_rc),
        wrapper_sha256=_sha256_file(wrapper),
        dry_run=dry_run,
        strict=strict,
        source_is_template=source_is_template,
        startup_patch_applied=startup_patch_applied,
        readiness=readiness_tuple,
        probe_commands=probe_commands,
        generated_rcS=generated_rcS,
        generated_rcS_sha256=_sha256_file(generated_rcS),
    )
    if write_evidence:
        launch_plan.write_evidence()
    if strict and not launch_plan.ready_to_launch:
        failed = [name for name, value in launch_plan.readiness if not value]
        raise Px4LaunchError("strict PX4 launch readiness failed: " + ", ".join(failed))
    return launch_plan


# Public aliases for callers that use the older planner naming.
patch_rcs = patch_rcS
plan_px4_launch = build_px4_launch_plan
prepare_px4_launch = build_px4_launch_plan
create_px4_launch_plan = build_px4_launch_plan
PX4LaunchPlan = Px4LaunchPlan
SimulatorPX4LaunchPlan = Px4LaunchPlan
SimulatorPx4LaunchPlan = Px4LaunchPlan


def verify_px4_launch_plan(plan: Px4LaunchPlan) -> bool:
    if not isinstance(plan, Px4LaunchPlan):
        raise TypeError("plan must be Px4LaunchPlan")
    return plan.verify()


__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_JOB_DIRECTORY_NAME",
    "DEFAULT_OVERLAY_DIRECTORY_NAME",
    "DEFAULT_WRAPPER_NAME",
    "DEFAULT_PATCHED_RC_NAME",
    "DEFAULT_GENERATED_RCS_NAME",
    "DEFAULT_RC_S_RELATIVE",
    "DEFAULT_RC_MAVLINK_RELATIVE",
    "Px4LaunchError",
    "PX4LaunchError",
    "SimulatorPx4LaunchError",
    "SimulatorPX4LaunchError",
    "Px4LaunchPlan",
    "PX4LaunchPlan",
    "SimulatorPx4LaunchPlan",
    "SimulatorPX4LaunchPlan",
    "patch_rc_mavlink",
    "patch_rcS",
    "patch_rcs",
    "build_px4_launch_plan",
    "plan_px4_launch",
    "prepare_px4_launch",
    "create_px4_launch_plan",
    "verify_px4_launch_plan",
]
