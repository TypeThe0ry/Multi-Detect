"""Side-effect-free process and MAVLink heartbeat readiness probes.

The simulator stack planner answers a *static* question (are the binaries and
job-local startup artifacts present?).  This module answers the runtime
question without owning process startup: are the processes supplied by the
caller still alive and, when requested, has the PX4 telemetry leg produced a
MAVLink ``HEARTBEAT``?  Keeping the probe independent from
``subprocess.Popen`` makes it safe to use from :class:`SimulatorOrchestrator`
and straightforward to exercise with fake processes and fake UDP sockets.

No service is started by this module.  A caller may provide an already-bound
socket, an iterable of datagrams, or a socket factory for an isolated loopback
endpoint.  The default endpoint parser accepts only literal loopback hosts;
hostnames and non-loopback addresses are rejected so a readiness check cannot
silently turn into a REAL link.
"""

from __future__ import annotations

import math
import socket
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from ..autonomy.serialization import canonical_sha256
from ..qgc_readonly_bridge import MSG_HEARTBEAT, MavlinkFrameDecoder

READINESS_SCHEMA_VERSION = 1
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
DEFAULT_HEARTBEAT_TIMEOUT_S = 0.0
DEFAULT_HEARTBEAT_POLL_INTERVAL_S = 0.02


class SimulatorReadinessError(ValueError):
    """Raised when a readiness probe configuration is unsafe or malformed."""


def _finite_nonnegative(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SimulatorReadinessError(f"{name} must be a finite non-negative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise SimulatorReadinessError(f"{name} must be a finite non-negative number")
    return result


def _port(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise SimulatorReadinessError(f"{name} must be an integer in 1..65535")
    return value


def _loopback_host(value: object, *, name: str = "heartbeat host") -> str:
    if not isinstance(value, str) or value not in LOOPBACK_HOSTS:
        raise SimulatorReadinessError(f"{name} must be literal loopback 127.0.0.1 or ::1")
    return value


def parse_readiness_endpoint(value: str | tuple[str, int] | None) -> tuple[str, int] | None:
    """Parse and validate a literal loopback UDP endpoint.

    Accepted spellings are ``udp://127.0.0.1:14541``,
    ``udpout:127.0.0.1:14541``, ``127.0.0.1:14541`` and bracketed IPv6.  A
    hostname such as ``localhost`` is intentionally not accepted: the
    isolation contract requires the address itself to be unambiguous.
    """

    if value is None:
        return None
    if isinstance(value, tuple):
        if len(value) != 2:
            raise SimulatorReadinessError("heartbeat endpoint tuple must be (host, port)")
        return _loopback_host(value[0]), _port(value[1], name="heartbeat port")
    if not isinstance(value, str) or not value.strip():
        raise SimulatorReadinessError("heartbeat endpoint must be a non-empty string or tuple")
    text = value.strip()
    host: str | None = None
    port_text: str | None = None
    parsed = urlparse(text)
    if parsed.scheme in {"udp", "udpout"}:
        # ``udpout:127.0.0.1:14541`` is parsed as a path by urllib, while
        # ``udp://...`` exposes hostname/port normally.
        if parsed.hostname is not None:
            host = parsed.hostname
            port_text = None if parsed.port is None else str(parsed.port)
        else:
            text = parsed.path
    if host is None:
        if text.startswith("["):
            closing = text.find("]")
            if closing < 0 or closing + 2 > len(text) or text[closing + 1] != ":":
                raise SimulatorReadinessError("invalid bracketed heartbeat endpoint")
            host = text[1:closing]
            port_text = text[closing + 2 :]
        else:
            # Literal IPv4 is the normal path.  Splitting from the right also
            # keeps an unbracketed IPv6 address deterministic (it will fail
            # the literal-host check unless bracketed).
            if text.count(":") != 1:
                raise SimulatorReadinessError("heartbeat endpoint must use host:port or [::1]:port")
            host, port_text = text.rsplit(":", 1)
    if port_text is None or not port_text.isdigit():
        raise SimulatorReadinessError("heartbeat endpoint port is invalid")
    return _loopback_host(host), _port(int(port_text), name="heartbeat port")


def _endpoint_text(endpoint: tuple[str, int] | None) -> str | None:
    if endpoint is None:
        return None
    host, port = endpoint
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _source_is_loopback(source: object) -> bool:
    return (
        isinstance(source, tuple)
        and len(source) >= 2
        and isinstance(source[0], str)
        and source[0] in LOOPBACK_HOSTS
    )


@dataclass(frozen=True, slots=True)
class SimulatorProcessReadiness:
    """One immutable process liveness observation."""

    name: str
    required: bool
    present: bool
    running: bool
    pid: int | None = None
    returncode: int | None = None
    poll_error: str | None = None
    checked_monotonic_s: float = 0.0

    @property
    def healthy(self) -> bool:
        return self.present and self.running and self.poll_error is None

    def to_mapping(self) -> dict[str, object]:
        return {
            "name": self.name,
            "required": self.required,
            "present": self.present,
            "running": self.running,
            "healthy": self.healthy,
            "pid": self.pid,
            "returncode": self.returncode,
            "poll_error": self.poll_error,
            "checked_monotonic_s": round(float(self.checked_monotonic_s), 6),
        }


@dataclass(frozen=True, slots=True)
class SimulatorHeartbeatObservation:
    """Counters and identity evidence collected from a MAVLink telemetry leg."""

    endpoint: str | None = None
    source: str = "none"
    datagrams_received: int = 0
    frames_received: int = 0
    heartbeat_frames: int = 0
    rejected_frames: int = 0
    discarded_bytes: int = 0
    source_system_ids: tuple[int, ...] = ()
    source_component_ids: tuple[int, ...] = ()
    first_seen_monotonic_s: float | None = None
    last_seen_monotonic_s: float | None = None
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "datagrams_received",
            "frames_received",
            "heartbeat_frames",
            "rejected_frames",
            "discarded_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SimulatorReadinessError(f"{name} must be a non-negative integer")
        if self.heartbeat_frames > self.frames_received:
            raise SimulatorReadinessError("heartbeat_frames cannot exceed frames_received")

    @property
    def observed(self) -> bool:
        return self.heartbeat_frames > 0

    def to_mapping(self) -> dict[str, object]:
        return {
            "endpoint": self.endpoint,
            "source": self.source,
            "datagrams_received": self.datagrams_received,
            "frames_received": self.frames_received,
            "heartbeat_frames": self.heartbeat_frames,
            "observed": self.observed,
            "rejected_frames": self.rejected_frames,
            "discarded_bytes": self.discarded_bytes,
            "source_system_ids": list(self.source_system_ids),
            "source_component_ids": list(self.source_component_ids),
            "first_seen_monotonic_s": self.first_seen_monotonic_s,
            "last_seen_monotonic_s": self.last_seen_monotonic_s,
            "errors": list(self.errors),
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object] | None,
        *,
        endpoint: str | None = None,
        source: str = "mapping",
    ) -> SimulatorHeartbeatObservation:
        """Adapt router evidence into the probe's stable heartbeat contract.

        ``SimulatorInteractiveRouter`` historically exposed message counters
        rather than a dedicated heartbeat field.  Accepting both forms lets
        old job evidence participate in the new readiness gate without
        pretending that a heartbeat was seen when the counter is absent.
        """

        if value is None:
            return cls(endpoint=endpoint, source=source)
        nested_endpoint = value.get("endpoint")
        selected_endpoint = (
            endpoint
            if endpoint is not None
            else (str(nested_endpoint) if nested_endpoint is not None else None)
        )
        if selected_endpoint is not None:
            # Keep this conversion strict even when the observation is built
            # directly (rather than through ``SimulatorReadinessProbe``).
            selected_endpoint = _endpoint_text(parse_readiness_endpoint(selected_endpoint))
        ids = value.get("px4_message_ids")
        heartbeat_frames = value.get("heartbeat_frames", value.get("px4_heartbeat_frames"))
        if heartbeat_frames is None and isinstance(ids, Mapping):
            heartbeat_frames = ids.get("0", ids.get(0, 0))
        try:
            heartbeat = max(0, int(heartbeat_frames or 0))
        except (TypeError, ValueError):
            heartbeat = 0
        frames_value = value.get("frames_received", value.get("px4_frames_received", heartbeat))
        try:
            frames = max(heartbeat, int(frames_value or 0))
        except (TypeError, ValueError):
            frames = heartbeat
        datagrams_value = value.get("datagrams_received", value.get("px4_datagrams_received", 0))
        try:
            datagrams = max(0, int(datagrams_value or 0))
        except (TypeError, ValueError):
            datagrams = 0
        return cls(
            endpoint=selected_endpoint,
            source=source,
            datagrams_received=datagrams,
            frames_received=frames,
            heartbeat_frames=heartbeat,
            rejected_frames=max(0, _int_or_zero(value.get("rejected_frames"))),
            discarded_bytes=max(0, _int_or_zero(value.get("discarded_bytes"))),
            source_system_ids=_int_tuple(value.get("source_system_ids")),
            source_component_ids=_int_tuple(value.get("source_component_ids")),
            first_seen_monotonic_s=_float_or_none(value.get("first_seen_monotonic_s")),
            last_seen_monotonic_s=_float_or_none(value.get("last_seen_monotonic_s")),
            errors=_string_tuple(value.get("errors")),
        )


def _int_or_zero(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float_or_none(value: object) -> float | None:
    try:
        result = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return result if result is None or math.isfinite(result) else None


def _int_tuple(value: object) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    result: set[int] = set()
    for item in value:
        try:
            result.add(int(item))
        except (TypeError, ValueError):
            continue
    return tuple(sorted(result))


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(sorted({str(item) for item in value if str(item)}))


class SimulatorHeartbeatObserver:
    """Decode injected MAVLink datagrams and retain heartbeat evidence."""

    def __init__(
        self,
        *,
        endpoint: str | tuple[str, int] | None = None,
        expected_system_id: int | None = 1,
        expected_component_ids: Iterable[int] | None = None,
        expected_source_port: int | None = None,
        decoder: MavlinkFrameDecoder | None = None,
        clock: Callable[[], float] = time.monotonic,
        source_label: str = "udp",
    ) -> None:
        self.endpoint = parse_readiness_endpoint(endpoint)
        if expected_system_id is not None:
            if isinstance(expected_system_id, bool) or not 1 <= int(expected_system_id) <= 255:
                raise SimulatorReadinessError("expected_system_id must be None or 1..255")
            expected_system_id = int(expected_system_id)
        self.expected_system_id = expected_system_id
        if expected_component_ids is None:
            self.expected_component_ids: frozenset[int] | None = None
        else:
            selected = frozenset(int(item) for item in expected_component_ids)
            if any(item < 1 or item > 255 for item in selected):
                raise SimulatorReadinessError("expected_component_ids must contain 1..255")
            self.expected_component_ids = selected
        if expected_source_port is not None:
            expected_source_port = _port(expected_source_port, name="expected source port")
        self.expected_source_port = expected_source_port
        self.decoder = decoder or MavlinkFrameDecoder()
        self.clock = clock
        self.source_label = str(source_label) or "udp"
        self._datagrams_received = 0
        self._frames_received = 0
        self._heartbeat_frames = 0
        self._rejected_frames = 0
        self._errors: list[str] = []
        self._source_system_ids: set[int] = set()
        self._source_component_ids: set[int] = set()
        self._first_seen: float | None = None
        self._last_seen: float | None = None

    def feed(
        self,
        datagram: bytes,
        *,
        source: tuple[str, int] | None = None,
        timestamp_s: float | None = None,
    ) -> int:
        """Feed one datagram and return the number of accepted heartbeats."""

        if not isinstance(datagram, (bytes, bytearray)):
            raise TypeError("heartbeat datagram must be bytes")
        if source is not None:
            if not _source_is_loopback(source):
                self._rejected_frames += 1
                self._errors.append("non_loopback_source")
                return 0
            if self.expected_source_port is not None:
                try:
                    source_port = int(source[1])
                except (TypeError, ValueError):
                    source_port = -1
                if source_port != self.expected_source_port:
                    self._rejected_frames += 1
                    self._errors.append("unexpected_source_port")
                    return 0
        self._datagrams_received += 1
        try:
            frames = self.decoder.feed(bytes(datagram))
        except Exception as exc:  # pragma: no cover - defensive decoder boundary.
            self._errors.append(f"decoder_error: {exc}")
            return 0
        self._frames_received += len(frames)
        heartbeat_count = 0
        for frame in frames:
            if frame.message_id != MSG_HEARTBEAT:
                continue
            if (
                self.expected_system_id is not None
                and frame.source_system_id != self.expected_system_id
            ):
                self._rejected_frames += 1
                self._errors.append("unexpected_source_system")
                continue
            if (
                self.expected_component_ids is not None
                and frame.source_component_id not in self.expected_component_ids
            ):
                self._rejected_frames += 1
                self._errors.append("unexpected_source_component")
                continue
            heartbeat_count += 1
            self._heartbeat_frames += 1
            self._source_system_ids.add(frame.source_system_id)
            self._source_component_ids.add(frame.source_component_id)
            stamp = self.clock() if timestamp_s is None else float(timestamp_s)
            if self._first_seen is None:
                self._first_seen = stamp
            self._last_seen = stamp
        return heartbeat_count

    @property
    def discarded_bytes(self) -> int:
        return int(self.decoder.discarded_bytes)

    def observation(self) -> SimulatorHeartbeatObservation:
        return SimulatorHeartbeatObservation(
            endpoint=_endpoint_text(self.endpoint),
            source=self.source_label,
            datagrams_received=self._datagrams_received,
            frames_received=self._frames_received,
            heartbeat_frames=self._heartbeat_frames,
            rejected_frames=self._rejected_frames,
            discarded_bytes=self.discarded_bytes,
            source_system_ids=tuple(sorted(self._source_system_ids)),
            source_component_ids=tuple(sorted(self._source_component_ids)),
            first_seen_monotonic_s=self._first_seen,
            last_seen_monotonic_s=self._last_seen,
            errors=tuple(sorted(set(self._errors))),
        )

    def evidence(self) -> dict[str, object]:
        return self.observation().to_mapping()


@dataclass(frozen=True, slots=True)
class SimulatorReadinessResult:
    """Immutable result emitted by :class:`SimulatorReadinessProbe`."""

    process_checks: tuple[SimulatorProcessReadiness, ...]
    heartbeat: SimulatorHeartbeatObservation
    required_processes: tuple[str, ...]
    require_heartbeat: bool
    endpoint: str | None
    elapsed_seconds: float
    errors: tuple[str, ...] = ()
    schema_version: int = READINESS_SCHEMA_VERSION

    @property
    def required_processes_present(self) -> bool:
        by_name = {item.name: item for item in self.process_checks}
        return all(name in by_name and by_name[name].present for name in self.required_processes)

    @property
    def required_processes_alive(self) -> bool:
        by_name = {item.name: item for item in self.process_checks}
        return all(name in by_name and by_name[name].healthy for name in self.required_processes)

    @property
    def process_ready(self) -> bool:
        return self.required_processes_present and self.required_processes_alive

    @property
    def heartbeat_ready(self) -> bool:
        return (not self.require_heartbeat) or self.heartbeat.observed

    @property
    def ready(self) -> bool:
        return self.process_ready and self.heartbeat_ready and not self.errors

    @property
    def gate_passed(self) -> bool:
        return self.ready

    @property
    def evidence_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event": "simulator_runtime_readiness",
            "ready": self.ready,
            "gate_passed": self.gate_passed,
            "required_processes": list(self.required_processes),
            "required_processes_present": self.required_processes_present,
            "required_processes_alive": self.required_processes_alive,
            "process_ready": self.process_ready,
            "require_heartbeat": self.require_heartbeat,
            "heartbeat_ready": self.heartbeat_ready,
            "endpoint": self.endpoint,
            "elapsed_seconds": round(max(0.0, float(self.elapsed_seconds)), 6),
            "processes": [item.to_mapping() for item in self.process_checks],
            "heartbeat": self.heartbeat.to_mapping(),
            "errors": list(self.errors),
            "truth_free": True,
        }

    def to_mapping_with_hash(self) -> dict[str, object]:
        result = self.to_mapping()
        result["sha256"] = self.evidence_hash
        return result

    @property
    def evidence(self) -> dict[str, object]:
        return self.to_mapping_with_hash()


def _managed_process_parts(value: object) -> tuple[object, bool]:
    """Extract process and required bit from an orchestrator managed record."""

    spec = getattr(value, "spec", None)
    process = getattr(value, "process", None)
    if spec is not None and process is not None:
        return process, bool(getattr(spec, "required", True))
    return value, True


def _normalise_processes(
    processes: Mapping[str, object] | Sequence[object] | Iterable[tuple[str, object]],
) -> tuple[tuple[str, object, bool], ...]:
    if isinstance(processes, Mapping):
        items: list[tuple[str, object]] = [(str(name), value) for name, value in processes.items()]
    else:
        items = []
        for item in processes:
            if isinstance(item, (tuple, list)) and len(item) == 2 and isinstance(item[0], str):
                items.append((item[0], item[1]))
                continue
            spec = getattr(item, "spec", None)
            name = getattr(spec, "name", None)
            if name is None:
                name = getattr(item, "name", None)
            if name is None:
                raise SimulatorReadinessError(
                    "process sequence entries must be (name, process) or managed records"
                )
            items.append((str(name), item))
    result: list[tuple[str, object, bool]] = []
    seen: set[str] = set()
    for name, raw in items:
        if not name.strip() or name in seen:
            raise SimulatorReadinessError("process names must be non-empty and unique")
        seen.add(name)
        process, required = _managed_process_parts(raw)
        result.append((name, process, required))
    return tuple(result)


def _poll_record(
    name: str,
    process: object,
    required: bool,
    *,
    now_s: float,
) -> SimulatorProcessReadiness:
    if process is None:
        return SimulatorProcessReadiness(
            name=name,
            required=required,
            present=False,
            running=False,
            checked_monotonic_s=now_s,
        )
    pid_value = getattr(process, "pid", None)
    try:
        pid = int(pid_value) if pid_value is not None else None
    except (TypeError, ValueError):
        pid = None
    poll = getattr(process, "poll", None)
    if not callable(poll):
        return SimulatorProcessReadiness(
            name=name,
            required=required,
            present=True,
            running=False,
            pid=pid,
            poll_error="process object has no callable poll()",
            checked_monotonic_s=now_s,
        )
    try:
        returncode = poll()
    except Exception as exc:  # pragma: no cover - defensive process boundary.
        return SimulatorProcessReadiness(
            name=name,
            required=required,
            present=True,
            running=False,
            pid=pid,
            poll_error=f"poll failed: {exc}",
            checked_monotonic_s=now_s,
        )
    normalized_returncode = _coerce_returncode(returncode)
    malformed_returncode = returncode is not None and normalized_returncode is None
    return SimulatorProcessReadiness(
        name=name,
        required=required,
        present=True,
        running=returncode is None,
        pid=pid,
        returncode=normalized_returncode,
        poll_error="poll returned a non-integer return code" if malformed_returncode else None,
        checked_monotonic_s=now_s,
    )


def _coerce_returncode(value: object) -> int | None:
    """Normalize a process return code while retaining odd fake values."""

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class SimulatorReadinessProbe:
    """Probe supplied processes and optional loopback MAVLink telemetry.

    ``probe`` never invokes ``Popen`` or any process-management API.  The
    optional socket path only receives datagrams from an endpoint supplied by
    the caller; tests can inject ``heartbeat_datagrams`` or ``socket_factory``
    to avoid operating-system networking altogether.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        socket_factory: Callable[..., Any] = socket.socket,
        poll_interval_s: float = DEFAULT_HEARTBEAT_POLL_INTERVAL_S,
    ) -> None:
        self.clock = clock
        self.sleep = sleep
        self.socket_factory = socket_factory
        self.poll_interval_s = _finite_nonnegative(poll_interval_s, name="poll_interval_s")

    def probe(
        self,
        processes: Mapping[str, object] | Sequence[object] | Iterable[tuple[str, object]],
        *,
        expected_processes: Sequence[str] | None = None,
        heartbeat_endpoint: str | tuple[str, int] | None = None,
        require_heartbeat: bool = False,
        timeout_s: float = DEFAULT_HEARTBEAT_TIMEOUT_S,
        heartbeat_socket: Any | None = None,
        heartbeat_datagrams: Iterable[bytes] | None = None,
        bind_socket: bool = True,
        close_socket: bool = False,
        expected_system_id: int | None = 1,
        expected_component_ids: Iterable[int] | None = None,
        expected_source_port: int | None = None,
        heartbeat_observation: SimulatorHeartbeatObservation | Mapping[str, object] | None = None,
    ) -> SimulatorReadinessResult:
        started = self.clock()
        timeout = _finite_nonnegative(timeout_s, name="timeout_s")
        if not isinstance(require_heartbeat, bool):
            raise SimulatorReadinessError("require_heartbeat must be bool")
        entries = _normalise_processes(processes)
        if expected_processes is None:
            required_names = tuple(name for name, _process, required in entries if required)
        else:
            required_names = tuple(str(name) for name in expected_processes)
            if len(set(required_names)) != len(required_names) or any(
                not name.strip() for name in required_names
            ):
                raise SimulatorReadinessError("expected_processes must be unique non-empty names")
        endpoint = parse_readiness_endpoint(heartbeat_endpoint)
        endpoint_text = _endpoint_text(endpoint)
        errors: list[str] = []
        before = self._records(entries, required_names)

        if isinstance(heartbeat_observation, SimulatorHeartbeatObservation):
            advertised_endpoint = heartbeat_observation.endpoint
            if advertised_endpoint is not None:
                advertised = parse_readiness_endpoint(advertised_endpoint)
                if endpoint is None:
                    endpoint = advertised
                    endpoint_text = _endpoint_text(endpoint)
                elif advertised != endpoint:
                    raise SimulatorReadinessError(
                        "heartbeat observation endpoint conflicts with probe endpoint"
                    )
            observation = heartbeat_observation
        elif isinstance(heartbeat_observation, Mapping):
            # Router/evidence mappings are an input boundary too.  Validate
            # their advertised endpoint instead of allowing a forged
            # non-loopback string to enter an otherwise loopback-only result.
            advertised_endpoint = heartbeat_observation.get("endpoint")
            if endpoint is None and advertised_endpoint is not None:
                endpoint = parse_readiness_endpoint(str(advertised_endpoint))
                endpoint_text = _endpoint_text(endpoint)
            elif endpoint is not None and advertised_endpoint is not None:
                advertised = parse_readiness_endpoint(str(advertised_endpoint))
                if advertised != endpoint:
                    raise SimulatorReadinessError(
                        "heartbeat observation endpoint conflicts with probe endpoint"
                    )
            observation = SimulatorHeartbeatObservation.from_mapping(
                heartbeat_observation,
                endpoint=endpoint_text,
                source="mapping",
            )
        else:
            observer = SimulatorHeartbeatObserver(
                endpoint=endpoint,
                expected_system_id=expected_system_id,
                expected_component_ids=expected_component_ids,
                expected_source_port=expected_source_port,
                clock=self.clock,
            )
            if heartbeat_datagrams is not None:
                for datagram in heartbeat_datagrams:
                    observer.feed(datagram, source=endpoint)
            socket_owned = False
            if endpoint is not None and timeout > 0 and heartbeat_socket is None:
                try:
                    heartbeat_socket = self._open_socket(endpoint)
                    socket_owned = True
                except (OSError, TypeError, ValueError) as exc:
                    errors.append(f"heartbeat_socket_open: {exc}")
            if heartbeat_socket is not None and endpoint is not None and bind_socket:
                try:
                    heartbeat_socket.bind(endpoint)
                except (AttributeError, OSError, TypeError, ValueError) as exc:
                    # An injected socket may already be bound.  Treat an
                    # explicit fake ``already_bound`` marker as success; a
                    # real bind error remains evidence, not a silent pass.
                    if not bool(getattr(heartbeat_socket, "already_bound", False)):
                        errors.append(f"heartbeat_socket_bind: {exc}")
            if heartbeat_socket is not None and timeout > 0 and not errors:
                self._collect_socket(
                    heartbeat_socket,
                    observer,
                    timeout_s=timeout,
                    endpoint=endpoint,
                    errors=errors,
                )
            observation = observer.observation()
            # Automatically-created probe sockets are always ours to close;
            # caller-injected sockets remain open unless explicitly requested.
            if heartbeat_socket is not None and (socket_owned or close_socket):
                try:
                    heartbeat_socket.close()
                except OSError:
                    pass

        # A caller-supplied observation is an input boundary, not an implicit
        # pass signal.  Preserve decoder/identity errors in the top-level
        # readiness result so ``require_heartbeat`` cannot be satisfied by a
        # forged ``heartbeat_frames`` counter that also reports rejected
        # frames or explicit errors.  Socket-path observations carry the same
        # information through ``SimulatorHeartbeatObserver``.
        errors.extend(str(item) for item in observation.errors if str(item))
        if observation.rejected_frames:
            errors.append("heartbeat_rejected_frames")

        after = self._records(entries, required_names)
        # A process that exits while the heartbeat receive loop is running is
        # represented by the post-wait snapshot.  Keep both timestamps in the
        # per-process record only through the latest observation so evidence is
        # stable and useful to callers polling repeatedly.
        del before
        elapsed = max(0.0, self.clock() - started)
        result = SimulatorReadinessResult(
            process_checks=after,
            heartbeat=observation,
            required_processes=required_names,
            require_heartbeat=require_heartbeat,
            endpoint=endpoint_text,
            elapsed_seconds=elapsed,
            errors=tuple(sorted(set(errors))),
        )
        return result

    def _records(
        self,
        entries: tuple[tuple[str, object, bool], ...],
        required_names: tuple[str, ...],
    ) -> tuple[SimulatorProcessReadiness, ...]:
        now = self.clock()
        by_name = {
            name: _poll_record(name, process, required, now_s=now)
            for name, process, required in entries
        }
        # Include explicit missing records so a failed expected PX4/Gazebo
        # launch is visible rather than disappearing from the evidence.
        for name in required_names:
            by_name.setdefault(
                name,
                SimulatorProcessReadiness(
                    name=name,
                    required=True,
                    present=False,
                    running=False,
                    checked_monotonic_s=now,
                ),
            )
        return tuple(by_name[name] for name in sorted(by_name))

    def _open_socket(self, endpoint: tuple[str, int]) -> Any:
        family = socket.AF_INET6 if ":" in endpoint[0] else socket.AF_INET
        try:
            channel = self.socket_factory(family, socket.SOCK_DGRAM)
        except TypeError:
            # Tiny test doubles often expose a zero-argument factory.  Keep
            # the production two-argument socket contract while accepting the
            # simpler injectable shape.
            channel = self.socket_factory()
        try:
            setblocking = getattr(channel, "setblocking", None)
            if callable(setblocking):
                setblocking(False)
        except OSError:
            pass
        return channel

    def _collect_socket(
        self,
        channel: Any,
        observer: SimulatorHeartbeatObserver,
        *,
        timeout_s: float,
        endpoint: tuple[str, int],
        errors: list[str],
    ) -> None:
        deadline = self.clock() + timeout_s
        max_iterations = max(
            1,
            int(math.ceil(timeout_s / max(self.poll_interval_s, 0.001))) + 2,
        )
        iterations = 0
        settimeout = getattr(channel, "settimeout", None)
        if callable(settimeout):
            try:
                settimeout(min(timeout_s, max(0.001, self.poll_interval_s or timeout_s)))
            except OSError:
                pass
        while self.clock() < deadline and iterations < max_iterations:
            iterations += 1
            recvfrom = getattr(channel, "recvfrom", None)
            if not callable(recvfrom):
                errors.append("heartbeat_socket has no recvfrom()")
                return
            try:
                received = recvfrom(65535)
            except (BlockingIOError, TimeoutError):
                if self.poll_interval_s <= 0:
                    # A zero poll interval is an explicit non-blocking mode;
                    # do not spin forever on an injected socket that has no
                    # datagrams available.
                    return
                self.sleep(min(self.poll_interval_s, max(0.0, deadline - self.clock())))
                continue
            except OSError as exc:
                errors.append(f"heartbeat_socket_recv: {exc}")
                return
            if received is None:
                return
            if isinstance(received, tuple) and len(received) >= 2:
                datagram, source = received[0], received[1]
            else:
                datagram, source = received, endpoint
            try:
                observer.feed(datagram, source=source, timestamp_s=self.clock())
            except (TypeError, ValueError) as exc:
                errors.append(f"heartbeat_datagram: {exc}")
                return
            if observer.observation().observed:
                return


# Friendly aliases used by downstream launchers and older experimental code.
ProcessReadiness = SimulatorProcessReadiness
HeartbeatObservation = SimulatorHeartbeatObservation
ReadinessResult = SimulatorReadinessResult
ProcessReadinessProbe = SimulatorReadinessProbe
SimulatorProcessReadinessProbe = SimulatorReadinessProbe
probe_simulator_readiness = SimulatorReadinessProbe().probe


__all__ = [
    "READINESS_SCHEMA_VERSION",
    "DEFAULT_HEARTBEAT_TIMEOUT_S",
    "DEFAULT_HEARTBEAT_POLL_INTERVAL_S",
    "SimulatorReadinessError",
    "parse_readiness_endpoint",
    "SimulatorProcessReadiness",
    "ProcessReadiness",
    "SimulatorHeartbeatObservation",
    "HeartbeatObservation",
    "SimulatorHeartbeatObserver",
    "SimulatorReadinessResult",
    "ReadinessResult",
    "SimulatorReadinessProbe",
    "ProcessReadinessProbe",
    "SimulatorProcessReadinessProbe",
    "probe_simulator_readiness",
]
