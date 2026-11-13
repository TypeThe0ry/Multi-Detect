"""One caller-authorized disposable-SIM setting and an honest GCS presence.

Neither parameter readback nor GCS presence grants flight/Mission authority.
No ARM, Mission, manual-control, battery override or second receive loop is
provided. The frozen owned lifecycle is inherited; all runtime outputs are
create-only. Historical bindings are evidence, not authentication or permits.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_V4 = ROOT / "scripts/onboard_teacher_gazebo_session_v4.py"
_raw = _V4.read_bytes()
if len(_raw) != 12186 or hashlib.sha256(_raw).hexdigest() != (
    "dd696b9ba0ef905d2e2fd2b49986bdbe16c156380fab2543258201763ed8015d"
):
    raise RuntimeError("Frozen teacher session V4 differs")

from scripts.diagnose_onboard_teacher_px4_events import strict_json  # noqa: E402
from scripts.onboard_teacher_gazebo_session import _connect  # noqa: E402
from scripts.onboard_teacher_gazebo_session_v4 import (  # noqa: E402
    ConcurrentTelemetryReceiverV4,
    OwnedTeacherGazeboSessionV4,
    ReadOnlyDiagnosticSender,
)
from scripts.run_simulator_docker import PINNED_IMAGE  # noqa: E402

CONTRACT = "multidetect.teacher-disposable-SIM-operator-v1"
PROTECTED_PARAMETERS = (
    "COM_ARM_WO_GPS",
    "COM_ARM_HFLT_CHK",
    "COM_MODE_ARM_CHK",
    "COM_RCL_EXCEPT",
    "NAV_RCL_ACT",
    "NAV_DLL_ACT",
    "COM_RC_LOSS_T",
    "COM_DL_LOSS_T",
)
SOURCE_NAMES = (
    "metadata",
    "metadata_extraction",
    "firmware_output",
    "parameter_output",
    "prior_evidence",
)
FALSE_CLAIMS = {
    name: False
    for name in (
        "source_authenticated",
        "context_qualified",
        "mission_approved",
        "arming_permission",
        "flight_authority",
        "flight_executed",
        "mission_uploaded",
        "teacher_expert_verified",
        "train_admitted",
        "model_loaded",
        "optimizer_performed",
        "final_release_allowed",
        "physical_endurance_verified",
        "battery_or_energy_override_performed",
    )
}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _now(clock):
    value = clock()
    _require(
        type(value) in (int, float) and math.isfinite(value) and value >= 0,
        "Finite actual clock required",
    )
    return value


def _write_new(path, raw):
    import os

    with Path(path).open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    with Path(path).open("rb") as stream:
        observed = stream.read(len(raw) + 1)
    _require(observed == raw, "Saved original output readback differs")
    return {
        "path": Path(path).resolve().as_posix(),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _json_new(path, value):
    return _write_new(
        path,
        (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode(),
    )


@dataclass(frozen=True, slots=True)
class SourceBinding:
    path: str
    bytes: int
    sha256: str

    def __post_init__(self):
        _require(
            type(self.path) is str and 0 < len(self.path) <= 4096, "Bounded source path required"
        )
        _require(
            type(self.bytes) is int and 0 <= self.bytes <= 2 * 1024 * 1024,
            "Small source byte budget required",
        )
        _require(
            type(self.sha256) is str and re.fullmatch("[0-9a-f]{64}", self.sha256),
            "Source SHA256 required",
        )

    @classmethod
    def from_mapping(cls, value):
        _require(
            type(value) is dict and set(value) == {"path", "bytes", "sha256"},
            "Exact binding required",
        )
        return cls(**value)

    def to_mapping(self):
        self.__post_init__()
        return {"path": self.path, "bytes": self.bytes, "sha256": self.sha256}

    def read(self, root):
        self.__post_init__()
        root = Path(root).resolve()
        path = (root / self.path).resolve()
        _require(path.is_relative_to(root), "Source must stay inside repository")
        before = path.stat()
        _require(before.st_size == self.bytes, "Bound source size differs")
        with path.open("rb") as stream:
            raw = stream.read(self.bytes + 1)
        after = path.stat()

        def identity(stat):
            return stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino

        _require(
            identity(before) == identity(after) and len(raw) == self.bytes,
            "Source changed during read",
        )
        _require(hashlib.sha256(raw).hexdigest() == self.sha256, "Bound source SHA differs")
        return raw


@dataclass(frozen=True, slots=True)
class DisposableSimOperatorIntent:
    experiment_id: str
    owned_container_name: str
    sources: tuple[SourceBinding, ...]
    explicitly_authorized: bool

    def __post_init__(self):
        _require(self.explicitly_authorized is True, "Explicit caller SIM intent required")
        _require(
            type(self.experiment_id) is str
            and re.fullmatch("[A-Za-z0-9_-]{1,96}", self.experiment_id),
            "Explicit experiment identity required",
        )
        _require(
            type(self.owned_container_name) is str
            and re.fullmatch("[A-Za-z0-9_.-]{1,128}", self.owned_container_name),
            "Exact owned container name required",
        )
        _require(
            type(self.sources) is tuple
            and len(self.sources) == len(SOURCE_NAMES)
            and all(type(s) is SourceBinding for s in self.sources),
            "All five original bindings required",
        )
        for binding in self.sources:
            binding.__post_init__()

    @classmethod
    def from_mapping(cls, value):
        _require(
            type(value) is dict
            and set(value)
            == {
                "contract",
                "experiment_id",
                "owned_container_name",
                "scope",
                "explicitly_authorized",
                "image",
                "change",
                "sources",
            },
            "Exact SIM intent schema required",
        )
        _require(
            value["contract"] == CONTRACT
            and value["scope"] == "owned-disposable-software-SIM-only"
            and value["explicitly_authorized"] is True
            and value["image"] == PINNED_IMAGE,
            "Only explicit disposable pinned SIM intent permitted",
        )
        change = value["change"]
        _require(
            type(change) is dict
            and set(change) == {"name", "expected_before", "requested_after"}
            and change["name"] == "COM_RC_IN_MODE"
            and type(change["expected_before"]) is int
            and change["expected_before"] == 1
            and type(change["requested_after"]) is int
            and change["requested_after"] == 4,
            "Only COM_RC_IN_MODE 1 to 4 allowed",
        )
        _require(
            type(value["sources"]) is dict and set(value["sources"]) == set(SOURCE_NAMES),
            "Five explicit source identities required",
        )
        return cls(
            value["experiment_id"],
            value["owned_container_name"],
            tuple(SourceBinding.from_mapping(value["sources"][name]) for name in SOURCE_NAMES),
            value["explicitly_authorized"],
        )

    def to_mapping(self):
        self.__post_init__()
        return {
            "contract": CONTRACT,
            "experiment_id": self.experiment_id,
            "owned_container_name": self.owned_container_name,
            "scope": "owned-disposable-software-SIM-only",
            "explicitly_authorized": self.explicitly_authorized,
            "image": PINNED_IMAGE,
            "change": {"name": "COM_RC_IN_MODE", "expected_before": 1, "requested_after": 4},
            "sources": {
                name: binding.to_mapping()
                for name, binding in zip(SOURCE_NAMES, self.sources, strict=True)
            },
        }


def parse_parameter_output(raw, name):
    _require(
        type(raw) is bytes
        and len(raw) <= 128 * 1024
        and name in (*PROTECTED_PARAMETERS, "COM_RC_IN_MODE"),
        "Exact bounded parameter output required",
    )
    pattern = (
        rf"^[x+*l ]*{re.escape(name)} \[\d+,\d+\] : "
        r"(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*$"
    )
    values = re.findall(pattern, raw.decode("utf-8", errors="strict"), re.MULTILINE)
    _require(len(values) == 1, "One actual named parameter line required; no default")
    value = float(values[0])
    _require(math.isfinite(value), "Parameter value must be finite")
    return value


def parse_firmware_output(raw):
    _require(type(raw) is bytes and len(raw) <= 128 * 1024, "Original firmware output required")
    text = raw.decode("utf-8", errors="strict")
    hashes = re.findall(r"^PX4 git-hash: ([0-9a-f]{40})\s*$", text, re.MULTILINE)
    versions = re.findall(r"^PX4 version: ([^\r\n]+)$", text, re.MULTILINE)
    _require(
        len(hashes) == len(versions) == 1
        and re.findall(r"^HW arch: (\S+)\s*$", text, re.MULTILINE) == ["PX4_SITL"],
        "Actual SITL firmware identity required",
    )
    return {"git_hash": hashes[0], "version_raw": versions[0]}


def admit_intent(intent, repository_root=ROOT):
    _require(type(intent) is DisposableSimOperatorIntent, "Typed explicit SIM intent required")
    intent.__post_init__()
    data = {
        name: binding.read(repository_root)
        for name, binding in zip(SOURCE_NAMES, intent.sources, strict=True)
    }
    metadata, extraction, evidence = (
        strict_json(data[name]) for name in ("metadata", "metadata_extraction", "prior_evidence")
    )
    _require(
        extraction["contract"] == "multidetect.teacher-pinned-PX4-metadata-extraction-v1"
        and extraction["image"] == PINNED_IMAGE
        and extraction["status"] == "pinned-image-metadata-extracted"
        and extraction["injected_runner"] is False
        and extraction["final_metadata_sources_reverified"] is True
        and extraction["cleanup_verified"] is True,
        "Completed actual pinned metadata extraction required",
    )
    expanded = SourceBinding.from_mapping(extraction["files"]["parameters.json.xz"]["expanded"])
    _require(
        expanded.read(repository_root) == data["metadata"], "Metadata extraction binding differs"
    )
    rows = [row for row in metadata["parameters"] if row.get("name") == "COM_RC_IN_MODE"]
    _require(
        len(rows) == 1
        and rows[0]["type"] == "Int32"
        and [v for v in rows[0]["values"] if type(v.get("value")) is int and v["value"] == 4]
        == [{"description": "Disable manual control", "value": 4}],
        "Pinned metadata must define mode4 as Disable manual control",
    )
    firmware = parse_firmware_output(data["firmware_output"])
    _require(
        evidence["contract"] == "multidetect.teacher-PX4-readonly-diagnostic-evidence-v1"
        and evidence["native_version_and_parameter_exit_codes_all_zero"] is True
        and evidence["reported_PX4_git_hash"] == firmware["git_hash"]
        and evidence["reported_PX4_version_raw"] == firmware["version_raw"]
        and type(evidence["observed_parameters"]["COM_RC_IN_MODE"]) is int
        and evidence["observed_parameters"]["COM_RC_IN_MODE"] == 1
        and parse_parameter_output(data["parameter_output"], "COM_RC_IN_MODE") == 1,
        "Historical actual firmware/parameter evidence differs",
    )
    return firmware


class SimOperator:
    def __init__(
        self, session, intent, output_directory, *, repository_root=ROOT, native_runner=None
    ):
        _require(isinstance(session, OwnedTeacherGazeboSessionV4), "Frozen owned session required")
        _require(type(intent) is DisposableSimOperatorIntent, "Typed intent required")
        self.session, self.intent, self.root = session, intent, Path(repository_root).resolve()
        self.directory = Path(output_directory).resolve()
        _require(
            self.directory.is_relative_to(self.root) and not self.directory.exists(),
            "Fresh contained operator output required",
        )
        self.runner, self._used, self._last_at = native_runner, False, None
        self._outputs = []
        self.report = {
            "contract": CONTRACT,
            "intent": intent.to_mapping(),
            "status": "planned",
            "injected_native_runner": native_runner is not None,
            "native_attempts": [],
            "parameter_change_attempts": 0,
            "parameter_change_attempts_scope": (
                "conservative_single_use_reservation_not_native_API_count"
            ),
            "parameter_change_API_attempted": False,
            "parameter_change_returned": False,
            "parameter_change_uncertain": False,
            "parameter_readback_verified": False,
            "protected_parameters_before": {},
            "protected_parameters_after": {},
            "sources_unchanged": False,
            "saved_outputs_unchanged": False,
            "error": None,
            "secondary_errors": [],
            "claims": dict(FALSE_CLAIMS),
        }

    def to_mapping(self):
        return copy.deepcopy(self.report)

    def _save(self, path, raw):
        binding = _write_new(path, raw)
        self._outputs.append(binding)
        return binding

    def _save_json(self, path, value):
        raw = (
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        ).encode()
        return self._save(path, raw)

    def _time(self):
        at = _now(self.session.clock.monotonic)
        _require(self._last_at is None or at >= self._last_at, "Operator clock regressed")
        self._last_at = at
        return at

    def _owner(self):
        session = self.session
        _require(
            session.execute is True
            and session._entered
            and not session._closed
            and threading.get_ident() == session._owner
            and session.receiver is None,
            "Opened same-thread owned session before RX transfer required",
        )
        _require(
            session.plan.docker_plan.image == PINNED_IMAGE
            and session.plan.docker_plan.container_name == self.intent.owned_container_name,
            "Exact disposable SIM intent owner differs",
        )
        identity = session._owned(session._inspect(session.container_id))
        _require(identity == session.container_id, "Current exact owned container changed")
        return identity

    def _native(self, args):
        allowed = [
            ["/opt/px4-gazebo/bin/px4-ver", "all"],
            ["/opt/px4-gazebo/bin/px4-param", "set", "COM_RC_IN_MODE", "4"],
        ]
        allowed.extend(
            ["/opt/px4-gazebo/bin/px4-param", "show", name]
            for name in ("COM_RC_IN_MODE", *PROTECTED_PARAMETERS)
        )
        _require(
            type(args) is list and args in allowed, "Only fixed scoped SIM CLI operations permitted"
        )
        identity = self._owner()
        argv = [self.session.plan.docker_plan.docker_executable, "exec", identity, *args]
        row = {
            "attempt_index": len(self.report["native_attempts"]),
            "command": argv,
            "container_id": identity,
            "clock_id": self.session.clock.session_id,
            "started_at_s": self._time(),
            "API_attempted": False,
            "API_returned": False,
            "native_exit_code": None,
            "error": None,
        }
        self.report["native_attempts"].append(row)
        row["before_API_receipt"] = self._save_json(
            self.directory / f"attempt-{row['attempt_index']:03d}.json", row
        )
        primary = None
        try:
            timeout = min(10, self.session._remaining())
            row["API_attempted"] = True
            if args[1:3] == ["set", "COM_RC_IN_MODE"]:
                self.report.update(
                    parameter_change_API_attempted=True, parameter_change_uncertain=True
                )
            result = (
                subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
                if self.runner is None
                else self.runner(argv, timeout_s=timeout)
            )
            row["API_returned"] = True
            _require(
                type(result.returncode) is int
                and type(result.stdout) is bytes
                and type(result.stderr) is bytes
                and len(result.stdout) + len(result.stderr) <= 128 * 1024,
                "Bounded original native bytes and exit required",
            )
            row["native_exit_code"] = result.returncode
            row["stdout"] = self._save(
                self.directory / f"native-{row['attempt_index']:03d}.stdout.bin", result.stdout
            )
            row["stderr"] = self._save(
                self.directory / f"native-{row['attempt_index']:03d}.stderr.bin", result.stderr
            )
            _require(result.returncode == 0, "Native SIM operation failed")
            self._owner()
            return result.stdout
        except BaseException as error:
            primary = error
            row["error"] = f"{type(error).__name__}: {error}"[:1024]
            raise
        finally:
            try:
                row["finished_at_s"] = self._time()
            except BaseException as secondary:
                row["finished_at_s"] = None
                row["secondary_error"] = f"{type(secondary).__name__}: {secondary}"[:1024]
                if primary is not None:
                    primary.add_note(row["secondary_error"])
                else:
                    raise

    def apply(self):
        _require(not self._used, "Operator is single-use; unknown/failed attempts never retry")
        self._used = True
        self.directory.mkdir(parents=False, exist_ok=False)
        primary = None
        try:
            expected = admit_intent(self.intent, self.root)
            self._save_json(self.directory / "intent.json", self.intent.to_mapping())
            current = parse_firmware_output(self._native(["/opt/px4-gazebo/bin/px4-ver", "all"]))
            _require(current == expected, "Current runtime firmware differs from admitted original")
            before = {
                name: parse_parameter_output(
                    self._native(["/opt/px4-gazebo/bin/px4-param", "show", name]), name
                )
                for name in ("COM_RC_IN_MODE", *PROTECTED_PARAMETERS)
            }
            self.report["protected_parameters_before"] = before
            _require(
                before["COM_RC_IN_MODE"] == 1, "Current mode must actually be1; no default/reset"
            )
            self.report["parameter_change_attempts"] = 1
            self._native(["/opt/px4-gazebo/bin/px4-param", "set", "COM_RC_IN_MODE", "4"])
            self.report["parameter_change_returned"] = True
            after = {
                name: parse_parameter_output(
                    self._native(["/opt/px4-gazebo/bin/px4-param", "show", name]), name
                )
                for name in ("COM_RC_IN_MODE", *PROTECTED_PARAMETERS)
            }
            self.report["protected_parameters_after"] = after
            _require(
                after["COM_RC_IN_MODE"] == 4
                and all(before[name] == after[name] for name in PROTECTED_PARAMETERS),
                "Readback target/protected safety settings differ",
            )
            self.report.update(parameter_readback_verified=True, parameter_change_uncertain=False)
            admit_intent(self.intent, self.root)
            self.report["sources_unchanged"] = True
            for binding in self._outputs:
                SourceBinding.from_mapping(binding).read(self.root)
            self.report.update(
                saved_outputs_unchanged=True,
                outputs_reverified=copy.deepcopy(self._outputs),
                status="SIM-configuration-readback-verified",
            )
        except BaseException as error:
            primary = error
            self.report.update(status="failed", error=f"{type(error).__name__}: {error}"[:1024])
            raise
        finally:
            try:
                self.report["receipt"] = _json_new(
                    self.directory / "operator-report.json", self.report
                )
            except BaseException as error:
                self.report["secondary_errors"].append(f"{type(error).__name__}: {error}"[:1024])
                self.report["status"] = "failed"
                if primary is not None:
                    primary.add_note(self.report["secondary_errors"][-1])
                else:
                    raise
        return self.to_mapping()


class GcsHeartbeatTicker:
    def __init__(self, connection, clock, clock_id, remaining):
        self.connection, self.clock, self.clock_id, self.remaining = (
            connection,
            clock,
            clock_id,
            remaining,
        )
        self._owner, self._last_at, self._last_sent = None, None, None
        self._rows, self._lock = [], threading.Lock()

    def to_mapping(self):
        with self._lock:
            return copy.deepcopy(self._rows)

    def tick(self):
        with self._lock:
            owner = threading.get_ident()
            if self._owner is None:
                self._owner = owner
            _require(self._owner == owner, "GCS heartbeat has one RX/send owner")
            at = _now(self.clock)
            _require(self._last_at is None or at >= self._last_at, "GCS clock regressed")
            self._last_at = at
            if self._last_sent is not None and at - self._last_sent < 1:
                return
            _require(len(self._rows) < 300, "GCS heartbeat budget exhausted")
            mav = self.connection.mav
            _require(
                type(mav.srcSystem) is int
                and type(mav.srcComponent) is int
                and (mav.srcSystem, mav.srcComponent) == (255, 190),
                "Actual GCS255/190 source required",
            )
            self.remaining()
            row = {
                "attempt_index": len(self._rows),
                "source_system_id": 255,
                "source_component_id": 190,
                "API_owner_thread_id": owner,
                "API": "heartbeat_send",
                "args": [6, 8, 0, 0, 4, 3],
                "clock_id": self.clock_id,
                "called_at_s": at,
                "API_returned": False,
                "datagram_delivery_verified": False,
                "error": None,
                "flight_authority": False,
            }
            self._rows.append(row)
            self._last_sent = at
        try:
            mav.heartbeat_send(6, 8, 0, 0, 4, 3)
            with self._lock:
                row["API_returned"] = True
                returned = _now(self.clock)
                _require(returned >= at, "GCS clock regressed after API")
                self._last_at = returned
                row["returned_at_s"] = returned
        except BaseException as error:
            with self._lock:
                row["error"] = f"{type(error).__name__}: {error}"[:1024]
            raise


class OwnedTeacherGazeboSessionV5(OwnedTeacherGazeboSessionV4):
    """Instance-only GCS identity projection, same unique RX and owned cleanup."""

    def __init__(self, *args, **kwargs):
        original = kwargs.pop("connection_factory", None)
        factory = _connect if original is None else original
        self._injected_connection = original is not None

        def connect(endpoint, **options):
            _require(
                options == {"source_system": 250, "source_component": 191, "autoreconnect": False},
                "Frozen connection arguments differ",
            )
            connection = factory(
                endpoint, source_system=255, source_component=190, autoreconnect=False
            )
            try:
                _require(
                    type(connection.mav.srcSystem) is int
                    and type(connection.mav.srcComponent) is int
                    and (connection.mav.srcSystem, connection.mav.srcComponent) == (255, 190),
                    "Actual GCS connection source identity differs",
                )
            except BaseException as error:
                try:
                    connection.close()
                except BaseException as secondary:
                    error.add_note(f"GCS connection close failed: {secondary}")
                raise
            return connection

        super().__init__(*args, connection_factory=connect, **kwargs)
        self._gcs = None

    def start_receiver(self, queue_capacity=256):
        _require(
            self.receiver is None
            and threading.get_ident() == self._owner
            and self.execute
            and not self._closed
            and self.connection is not None,
            "Opened one main-thread receiver transfer required",
        )
        self._gcs = GcsHeartbeatTicker(
            self.connection, self.clock.monotonic, self.clock.session_id, self._remaining
        )

        def reader(timeout):
            _require(
                self.connection.recv_match == self._receive and not self._closed,
                "Original transferred receive differs",
            )
            self._gcs.tick()
            result = self._receive(blocking=True, timeout=min(timeout, self._remaining()))
            self._remaining()
            return result

        self._diagnostic_sender = ReadOnlyDiagnosticSender(
            self.connection, self.clock.monotonic, self.clock.session_id, self._remaining
        )
        self.receiver = ConcurrentTelemetryReceiverV4(
            reader,
            self.clock.monotonic,
            request_sender=self._diagnostic_sender,
            queue_capacity=queue_capacity,
        ).start()
        return self.receiver

    def to_mapping(self):
        return {
            **super().to_mapping(),
            "contract": "multidetect.onboard-teacher-gazebo-session-v5",
            "GCS_source_identity": {
                "system_id": 255,
                "component_id": 190,
                "vehicle_type": 6,
                "autopilot": 8,
            },
            "GCS_connection_factory_injected": self._injected_connection,
            "GCS_heartbeat_API_attempts": [] if self._gcs is None else self._gcs.to_mapping(),
            "flight_authority": False,
        }
