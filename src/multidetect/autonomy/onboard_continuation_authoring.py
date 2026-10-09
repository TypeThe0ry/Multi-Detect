"""Two-phase, create-only original-host authoring provenance, without authority.

Call begin at ordinary compilation, before starting the Mission transaction.
Seal only after the original START_PERMITTED journal and binding are complete.
The runtime owns Clock and its explicit epoch; a clock, source name, hash or
file is not authentication. No source is automatically registered as trusted.
This tool is not connected to a real vehicle/transport and never proves flight.

Publication uses a same-directory fsynced temporary plus atomic create-only
hard-link, not replace. Unsupported filesystems fail instead of falling back to
overwrite. The host must protect the directory from concurrent hostile renames;
directory fsync is unavailable on Windows, so power-loss durability is not proven.
"""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
from dataclasses import dataclass, fields
from pathlib import Path
from uuid import UUID, uuid4

from .. import mission_planning, px4_mission
from ..clock import Clock
from ..mission_journal import (
    DurableMissionJournal,
    MissionJournalIdentity,
    MissionJournalRecord,
    MissionTransactionState,
)
from ..mission_planning import PlannedMissionCompilation, compile_planned_task_mission_from_runtime
from . import planner
from .contracts import ContextSnapshot, ResolvedTaskSpec
from .onboard_continuation_admission import AUTHORING_CONTRACT, ContinuationAuthoringEvidence
from .onboard_evidence_collector import OnboardMissionTelemetryBinding
from .onboard_history import OnboardInferenceScope
from .onboard_multimodal import OnboardMultimodalScope, Scope
from .policy_artifact import PolicyArtifact
from .serialization import (
    canonical_json,
    canonical_sha256,
    require_int,
    require_real,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)

BEGIN_CONTRACT = "multidetect.original-host-authoring-begin-v1"
SEAL_CONTRACT = "multidetect.original-host-authoring-seal-v1"
_MAX_BEGIN_BYTES = 128 * 1024
_MAX_JOURNAL_BYTES = 256 * 1024
_MAX_SEAL_BYTES = 1024 * 1024
_BOUNDARY = {
    "execution_authority": False,
    "transport_created": False,
    "authenticated_source_verified": False,
    "clock_authentication_verified": False,
    "actual_flight_verified": False,
    "automatic_trust_registration": False,
    "host_transport_integration_implemented": False,
    "power_loss_durability_verified": False,
}


class OriginalHostAuthoringError(ValueError):
    """Inconsistent phase, original bytes, identity, clock or safe output."""


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _boundary(value) -> None:
    strict_object(value, name="authoring boundary", required=frozenset(_BOUNDARY))
    if any(item is not False for item in value.values()):
        raise OriginalHostAuthoringError("authoring boundary cannot grant or claim authentication")


def _scope(scope: Scope) -> OnboardMultimodalScope:
    if isinstance(scope, OnboardInferenceScope):
        scope = OnboardMultimodalScope.from_inference_scope(scope)
    if not isinstance(scope, OnboardMultimodalScope) or (
        scope.vehicle_class != "multirotor"
        or scope.home_sha256 is None
        or scope.airframe_profile_sha256 is None
    ):
        raise OriginalHostAuthoringError("known multirotor profile/home scope required")
    return scope


def _scope_from_mapping(value) -> OnboardMultimodalScope:
    names = {field.name for field in fields(OnboardMultimodalScope)}
    raw = strict_object(
        value,
        name="original scope",
        required=frozenset(names | {"schema_version", "contract", "clock_semantics"}),
    )
    result = _scope(OnboardMultimodalScope(**{name: raw[name] for name in names}))
    if result.to_mapping() != raw:
        raise OriginalHostAuthoringError("original scope mapping differs")
    return result


def _clock(clock: Clock, epoch: str, scope: OnboardMultimodalScope) -> float:
    if not isinstance(clock, Clock):
        raise TypeError("clock must implement the host Clock protocol")
    if require_text(epoch, "clock_epoch") != scope.clock_epoch:
        raise OriginalHostAuthoringError("host clock epoch differs from scope")
    return require_real(clock.monotonic(), "actual host clock", minimum=0)


def _source_snapshot() -> list[dict]:
    sources = (
        Path(__file__),
        Path(mission_planning.__file__),
        Path(px4_mission.__file__),
        Path(planner.__file__),
    )
    return [
        {
            "path": str(path.resolve()),
            "bytes": path.stat().st_size,
            "sha256": _sha(path.read_bytes()),
        }
        for path in sources
    ]


def _source_check(value) -> None:
    if not isinstance(value, list) or value != _source_snapshot():
        raise OriginalHostAuthoringError("actual imported authoring/compiler source drift")


def _non_link(path: Path) -> bool:
    return not path.is_symlink() and not getattr(path, "is_junction", lambda: False)()


def _directory(value: Path) -> Path:
    if not isinstance(value, Path) or not value.is_absolute() or value.parent == value:
        raise OriginalHostAuthoringError(
            "output_dir must be a specific existing absolute directory"
        )
    for path in (value, *value.parents):
        if not _non_link(path):
            raise OriginalHostAuthoringError("output path must not traverse symlink/junction")
    if not value.is_dir() or value.resolve(strict=True) != value:
        raise OriginalHostAuthoringError("output directory must exist and be canonical")
    return value


def _destination(directory: Path, name: str) -> Path:
    directory = _directory(directory)
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 128
        or name in {".", ".."}
        or name[-1] in {".", " "}
        or any(ord(char) < 32 or char in '<>:"/\\|?*' for char in name)
    ):
        raise OriginalHostAuthoringError("output name must be a bounded single safe filename")
    stem = name.split(".", 1)[0].upper()
    if stem in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        raise OriginalHostAuthoringError("reserved output filename")
    output = directory / name
    if output.exists() or not _non_link(output):
        raise FileExistsError(f"create-only authoring output already exists: {output}")
    return output


def _publish(directory: Path, name: str, payload: bytes) -> Path:
    """Fully written bytes become visible atomically, never replace an output."""
    output = _destination(directory, name)
    if type(payload) is not bytes or not 0 < len(payload) <= _MAX_SEAL_BYTES:
        raise OriginalHostAuthoringError("published bytes exceed bound")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".authoring-", dir=directory)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _destination(directory, name)
        os.link(temporary, output)  # Atomic no-replace commit; no overwrite fallback.
        if os.name != "nt":
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        if output.read_bytes() != payload:
            raise OriginalHostAuthoringError("published output changed after atomic creation")
    finally:
        # This exact temporary is created by this call; existing user outputs are never removed.
        temporary.unlink(missing_ok=True)
    return output


def _read_original(path: Path, payload: bytes, expected_sha: str, maximum: int) -> dict:
    if not isinstance(path, Path) or not path.is_absolute() or not _non_link(path):
        raise OriginalHostAuthoringError("original source path must be regular non-symlink")
    if type(payload) is not bytes or not 0 < len(payload) <= maximum:
        raise OriginalHostAuthoringError("original payload must be bounded immutable bytes")
    require_sha256(expected_sha, "original bytes SHA")
    if _sha(payload) != expected_sha or path.stat().st_size != len(payload):
        raise OriginalHostAuthoringError("original source bytes/size/SHA mismatch")
    if path.read_bytes() != payload:
        raise OriginalHostAuthoringError("original source changed")
    return dict(strict_json_loads(payload, name="original host authoring"))


@dataclass(frozen=True, slots=True)
class OriginalHostAuthoringRecord:
    path: Path
    source_receipt_bytes: bytes
    source_receipt_sha256: str

    def __post_init__(self) -> None:
        self.to_mapping()

    def to_mapping(self) -> dict:
        return self._validate(
            _read_original(
                self.path, self.source_receipt_bytes, self.source_receipt_sha256, _MAX_BEGIN_BYTES
            )
        )

    @staticmethod
    def _validate(value) -> dict:
        raw = strict_object(
            value,
            name="original host authoring begin",
            required=frozenset(
                {
                    "schema_version",
                    "contract",
                    "authoring_id",
                    "source_id",
                    "observed_at_s",
                    "compilation_completed_at_s",
                    "clock_epoch",
                    "scope",
                    "task",
                    "context",
                    "runtime",
                    "policy_artifact",
                    "compilation",
                    "source_files",
                    "boundary",
                }
            ),
        )
        if (
            type(raw["schema_version"]) is not int
            or raw["schema_version"] != 1
            or raw["contract"] != BEGIN_CONTRACT
        ):
            raise OriginalHostAuthoringError("unsupported begin contract/boundary")
        _boundary(raw["boundary"])
        if str(UUID(raw["authoring_id"])) != raw["authoring_id"]:
            raise OriginalHostAuthoringError("authoring_id must be a canonical UUID")
        if len(require_text(raw["source_id"], "source_id")) > 128:
            raise OriginalHostAuthoringError("source_id exceeds bound")
        scope = _scope_from_mapping(raw["scope"])
        if raw["clock_epoch"] != scope.clock_epoch:
            raise OriginalHostAuthoringError("original host epoch differs from scope")
        task = ResolvedTaskSpec.from_mapping(raw["task"])
        context = ContextSnapshot.from_mapping(raw["context"])
        scope.validate_context(context)
        artifact = PolicyArtifact.from_mapping(raw["policy_artifact"])
        if task.task_id != scope.selected_task_id or (
            artifact.airframe_profile_hash != scope.airframe_profile_sha256
        ):
            raise OriginalHostAuthoringError("task/artifact/profile differs from scope")
        at_s = require_real(
            raw["observed_at_s"], "begin observed_at_s", minimum=context.captured_at_s
        )
        require_real(raw["compilation_completed_at_s"], "compile completed_at_s", minimum=at_s)
        compilation = PlannedMissionCompilation.from_mapping(raw["compilation"])
        actual = compile_planned_task_mission_from_runtime(
            task,
            context,
            "multirotor",
            runtime=raw["runtime"],
            airframe_profile_hash=scope.airframe_profile_sha256,
            mission_id=compilation.compiled_mission.executable_mission.mission_id,
        )
        if actual != compilation:
            raise OriginalHostAuthoringError(
                "original task/context/full runtime compilation differs"
            )
        _source_check(raw["source_files"])
        return dict(raw)

    @property
    def compilation(self) -> PlannedMissionCompilation:
        return PlannedMissionCompilation.from_mapping(self.to_mapping()["compilation"])

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @classmethod
    def from_path(cls, path: Path, *, expected_sha256: str) -> OriginalHostAuthoringRecord:
        if (
            not isinstance(path, Path)
            or not _non_link(path)
            or path.stat().st_size > _MAX_BEGIN_BYTES
        ):
            raise OriginalHostAuthoringError("unsafe or oversized original begin file")
        return cls(path, path.read_bytes(), expected_sha256)


def begin_original_host_authoring(
    task: ResolvedTaskSpec,
    context: ContextSnapshot,
    runtime: object,
    scope: Scope,
    policy_artifact: PolicyArtifact,
    *,
    clock: Clock,
    clock_epoch: str,
    source_id: str,
    mission_id: str,
    output_dir: Path,
    record_name: str,
) -> OriginalHostAuthoringRecord:
    """Compile and persist now, before passing .compilation to the transaction."""
    normalized = _scope(scope)
    at_s = _clock(clock, clock_epoch, normalized)
    normalized.validate_context(context)
    if context.captured_at_s > at_s:
        raise OriginalHostAuthoringError("original context is future to actual begin clock")
    if not isinstance(task, ResolvedTaskSpec) or not isinstance(policy_artifact, PolicyArtifact):
        raise TypeError("begin requires typed original task and shared policy artifact")
    if task.task_id != normalized.selected_task_id or (
        policy_artifact.airframe_profile_hash != normalized.airframe_profile_sha256
    ):
        raise OriginalHostAuthoringError("task/artifact/profile differs from original scope")
    source_id = require_text(source_id, "source_id")
    if len(source_id) > 128:
        raise OriginalHostAuthoringError("source_id exceeds bound")
    _destination(output_dir, record_name)
    source_files = _source_snapshot()
    runtime = strict_json_loads(canonical_json(runtime), name="original full runtime")
    compilation = compile_planned_task_mission_from_runtime(
        task,
        context,
        "multirotor",
        runtime=runtime,
        airframe_profile_hash=normalized.airframe_profile_sha256,
        mission_id=require_text(mission_id, "mission_id"),
    )
    completed_at_s = _clock(clock, clock_epoch, normalized)
    if completed_at_s < at_s:
        raise OriginalHostAuthoringError("host clock regressed during original compilation")
    _source_check(source_files)
    document = {
        "schema_version": 1,
        "contract": BEGIN_CONTRACT,
        "authoring_id": str(uuid4()),
        "source_id": source_id,
        "observed_at_s": at_s,
        "compilation_completed_at_s": completed_at_s,
        "clock_epoch": clock_epoch,
        "scope": normalized.to_mapping(),
        "task": task.to_mapping(),
        "context": context.to_mapping(),
        "runtime": runtime,
        "policy_artifact": policy_artifact.to_mapping(),
        "compilation": compilation.to_mapping(),
        "source_files": source_files,
        "boundary": dict(_BOUNDARY),
    }
    payload = canonical_json(document).encode()
    if len(payload) > _MAX_BEGIN_BYTES:
        raise OriginalHostAuthoringError("original authoring begin exceeds byte bound")
    OriginalHostAuthoringRecord._validate(document)
    path = _publish(output_dir, record_name, payload)
    return OriginalHostAuthoringRecord(path, payload, _sha(payload))


def _journal(record: dict, binding: OnboardMissionTelemetryBinding, now_s: float):
    if not isinstance(binding, OnboardMissionTelemetryBinding):
        raise TypeError("seal requires actual typed original Mission telemetry binding")
    binding.__post_init__()
    scope = _scope_from_mapping(record["scope"])
    compilation = PlannedMissionCompilation.from_mapping(record["compilation"])
    context = ContextSnapshot.from_mapping(record["context"])
    if (
        binding.clock_epoch != scope.clock_epoch
        or binding.mission != compilation.compiled_mission.executable_mission
        or binding.task.to_mapping() != record["task"]
        or not (record["compilation_completed_at_s"] <= binding.bound_at_s <= now_s)
    ):
        raise OriginalHostAuthoringError("original binding/Mission/task/epoch/time differs")
    path = binding.journal_path
    if not _non_link(path) or path.stat().st_size > _MAX_JOURNAL_BYTES:
        raise OriginalHostAuthoringError("unsafe or oversized original journal")
    original = path.read_bytes()
    if len(original) != binding.journal_bytes or _sha(original) != binding.journal_sha256:
        raise OriginalHostAuthoringError("original journal bytes/SHA differ")
    with DurableMissionJournal.open(path) as journal:
        if (
            journal.recovery_required
            or journal.state is not MissionTransactionState.START_PERMITTED
        ):
            raise OriginalHostAuthoringError("intact original START_PERMITTED journal required")
        identity = journal.identity
        _, safety = mission_planning.parse_mission_compile_runtime(record["runtime"], "multirotor")
        expected = (
            identity.executable_mission_hash == compilation.executable_mission_hash
            and identity.protocol_hash == compilation.wire_hash
            and identity.mission_item_count == len(compilation.compiled_mission.wire_items)
            and identity.airframe_adapter_id == binding.mission.airframe_adapter_id
            and identity.bindings.prompt_hash == scope.prompt_sha256
            and identity.bindings.task_spec_hash == binding.task.canonical_hash
            and identity.bindings.context_snapshot_hash == context.canonical_hash
            and identity.bindings.plan_hash == compilation.plan.canonical_hash
            and identity.bindings.airframe_profile_hash == scope.airframe_profile_sha256
            and identity.bindings.geofence_hash == safety.geofence_hash
            and identity.bindings.vehicle_identity_hash == binding.vehicle_identity_sha256
        )
        if not expected:
            raise OriginalHostAuthoringError("journal identity differs from original authoring")
        identity_mapping = identity.to_mapping()
    if path.read_bytes() != original:
        raise OriginalHostAuthoringError("original journal changed during seal")
    return original, identity_mapping


def _encoded(payload: bytes) -> dict:
    return {
        "bytes": len(payload),
        "sha256": _sha(payload),
        "original_bytes_base64": base64.b64encode(payload).decode("ascii"),
    }


@dataclass(frozen=True, slots=True)
class SealedOriginalHostAuthoring:
    path: Path
    source_receipt_bytes: bytes
    source_receipt_sha256: str

    def __post_init__(self) -> None:
        self.to_mapping()

    def to_mapping(self) -> dict:
        raw = strict_object(
            _read_original(
                self.path, self.source_receipt_bytes, self.source_receipt_sha256, _MAX_SEAL_BYTES
            ),
            name="sealed original host authoring",
            required=frozenset(
                {
                    "schema_version",
                    "contract",
                    "sealed_at_s",
                    "clock_epoch",
                    "begin_original",
                    "journal_original",
                    "journal_identity",
                    "mission_binding",
                    "evidence_original",
                    "boundary",
                    "source_authentication",
                    "clock_semantics",
                }
            ),
        )
        if (
            type(raw["schema_version"]) is not int
            or raw["schema_version"] != 1
            or (
                raw["contract"] != SEAL_CONTRACT
                or raw["source_authentication"] != "host_registry_required_not_proven_by_hash"
                or raw["clock_semantics"] != "explicit_host_epoch_not_sensor_clock_claim"
            )
        ):
            raise OriginalHostAuthoringError("unsupported seal contract/boundary")
        _boundary(raw["boundary"])
        for name in ("begin_original", "journal_original", "evidence_original"):
            encoded = strict_object(
                raw[name],
                name=name,
                required=frozenset({"bytes", "sha256", "original_bytes_base64"}),
            )
            payload = base64.b64decode(encoded["original_bytes_base64"], validate=True)
            require_int(encoded["bytes"], name + " bytes", minimum=1, maximum=_MAX_SEAL_BYTES)
            if _encoded(payload) != encoded:
                raise OriginalHostAuthoringError("embedded original bytes/SHA differ")
        evidence = self._evidence(raw)
        declaration = evidence.to_mapping()
        if raw["journal_original"]["sha256"] != declaration["parent_journal_sha256"]:
            raise OriginalHostAuthoringError("sealed journal/evidence hash differs")
        begin_bytes = base64.b64decode(
            raw["begin_original"]["original_bytes_base64"], validate=True
        )
        if len(begin_bytes) > _MAX_BEGIN_BYTES:
            raise OriginalHostAuthoringError("embedded begin exceeds bound")
        begin = OriginalHostAuthoringRecord._validate(strict_json_loads(begin_bytes))
        scope = _scope_from_mapping(begin["scope"])
        compilation = PlannedMissionCompilation.from_mapping(begin["compilation"])
        identity = MissionJournalIdentity.from_mapping(raw["journal_identity"])
        journal_bytes = base64.b64decode(
            raw["journal_original"]["original_bytes_base64"], validate=True
        )
        if len(journal_bytes) > _MAX_JOURNAL_BYTES:
            raise OriginalHostAuthoringError("embedded journal exceeds bound")
        rows = tuple(
            MissionJournalRecord.from_mapping(strict_json_loads(line))
            for line in journal_bytes.splitlines()
        )
        if (
            not rows
            or len(rows) > 256
            or (
                rows[0].payload.get("identity") != identity.to_mapping()
                or rows[-1].to_state is not MissionTransactionState.START_PERMITTED
            )
        ):
            raise OriginalHostAuthoringError("embedded original journal state/identity differs")
        for index, row in enumerate(rows):
            if (
                row.sequence != index
                or row.identity_hash != identity.canonical_hash
                or (
                    row.previous_record_hash
                    != (None if index == 0 else rows[index - 1].record_hash)
                )
            ):
                raise OriginalHostAuthoringError("embedded journal hash-chain differs")
        expected = {
            "schema_version": 1,
            "contract": AUTHORING_CONTRACT,
            "source_id": begin["source_id"],
            "observed_at_s": begin["observed_at_s"],
            "scope_sha256": scope.canonical_hash,
            "parent_mission_sha256": compilation.executable_mission_hash,
            "parent_wire_sha256": compilation.wire_hash,
            "journal_context_sha256": canonical_sha256(begin["context"]),
            "parent_journal_sha256": raw["journal_original"]["sha256"],
            "parent_transaction_id": identity.transaction_id,
            "task_sha256": canonical_sha256(begin["task"]),
            "runtime_sha256": canonical_sha256(begin["runtime"]),
            "policy_artifact_sha256": PolicyArtifact.from_mapping(
                begin["policy_artifact"]
            ).canonical_hash,
        }
        if declaration != expected or raw["clock_epoch"] != scope.clock_epoch:
            raise OriginalHostAuthoringError(
                "sealed declaration differs from original begin lineage"
            )
        context = ContextSnapshot.from_mapping(begin["context"])
        _, safety = mission_planning.parse_mission_compile_runtime(begin["runtime"], "multirotor")
        if (
            identity.executable_mission_hash != compilation.executable_mission_hash
            or identity.protocol_hash != compilation.wire_hash
            or identity.bindings.context_snapshot_hash != context.canonical_hash
            or identity.bindings.task_spec_hash != expected["task_sha256"]
            or identity.bindings.prompt_hash != scope.prompt_sha256
            or identity.bindings.plan_hash != compilation.plan.canonical_hash
            or identity.bindings.airframe_profile_hash != scope.airframe_profile_sha256
            or identity.bindings.geofence_hash != safety.geofence_hash
        ):
            raise OriginalHostAuthoringError("embedded journal original authoring identity differs")
        binding = strict_object(
            raw["mission_binding"],
            name="original Mission binding",
            required=frozenset(
                {
                    "mission_sha256",
                    "task_sha256",
                    "vehicle_identity_sha256",
                    "bound_at_s",
                    "clock_epoch",
                }
            ),
        )
        if (
            binding["mission_sha256"] != expected["parent_mission_sha256"]
            or binding["task_sha256"] != expected["task_sha256"]
            or binding["vehicle_identity_sha256"] != identity.bindings.vehicle_identity_hash
            or binding["clock_epoch"] != scope.clock_epoch
        ):
            raise OriginalHostAuthoringError("sealed Mission binding identity differs")
        bound_at_s = require_real(
            binding["bound_at_s"],
            "original binding time",
            minimum=begin["compilation_completed_at_s"],
        )
        require_real(raw["sealed_at_s"], "actual seal time", minimum=bound_at_s)
        return dict(raw)

    @staticmethod
    def _evidence(raw) -> ContinuationAuthoringEvidence:
        encoded = raw["evidence_original"]
        return ContinuationAuthoringEvidence(
            base64.b64decode(encoded["original_bytes_base64"], validate=True),
            encoded["sha256"],
        )

    @property
    def evidence(self) -> ContinuationAuthoringEvidence:
        return self._evidence(self.to_mapping())  # Trust remains empty.

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @classmethod
    def from_path(cls, path: Path, *, expected_sha256: str) -> SealedOriginalHostAuthoring:
        if (
            not isinstance(path, Path)
            or not _non_link(path)
            or path.stat().st_size > _MAX_SEAL_BYTES
        ):
            raise OriginalHostAuthoringError("unsafe or oversized original seal file")
        return cls(path, path.read_bytes(), expected_sha256)


def seal_original_host_authoring(
    record: OriginalHostAuthoringRecord,
    mission_binding: OnboardMissionTelemetryBinding,
    *,
    clock: Clock,
    clock_epoch: str,
    output_dir: Path,
    seal_name: str,
) -> SealedOriginalHostAuthoring:
    """Bind original transaction now; never accept/rewrite an earlier begin time."""
    if not isinstance(record, OriginalHostAuthoringRecord):
        raise TypeError("seal requires the original begin record")
    raw = record.to_mapping()
    scope = _scope_from_mapping(raw["scope"])
    now_s = _clock(clock, clock_epoch, scope)
    if now_s < raw["compilation_completed_at_s"]:
        raise OriginalHostAuthoringError("seal clock regressed before original compilation")
    _destination(output_dir, seal_name)
    original, identity = _journal(raw, mission_binding, now_s)
    declaration = {
        "schema_version": 1,
        "contract": AUTHORING_CONTRACT,
        "source_id": raw["source_id"],
        "observed_at_s": raw["observed_at_s"],
        "scope_sha256": scope.canonical_hash,
        "parent_mission_sha256": mission_binding.mission.canonical_hash,
        "parent_wire_sha256": raw["compilation"]["wire_hash"],
        "journal_context_sha256": canonical_sha256(raw["context"]),
        "parent_journal_sha256": mission_binding.journal_sha256,
        "parent_transaction_id": identity["transaction_id"],
        "task_sha256": mission_binding.task.canonical_hash,
        "runtime_sha256": canonical_sha256(raw["runtime"]),
        "policy_artifact_sha256": PolicyArtifact.from_mapping(
            raw["policy_artifact"]
        ).canonical_hash,
    }
    payload = canonical_json(declaration).encode()
    ContinuationAuthoringEvidence(payload, _sha(payload))
    document = {
        "schema_version": 1,
        "contract": SEAL_CONTRACT,
        "sealed_at_s": now_s,
        "clock_epoch": clock_epoch,
        "begin_original": _encoded(record.source_receipt_bytes),
        "journal_original": _encoded(original),
        "journal_identity": identity,
        "mission_binding": {
            "mission_sha256": mission_binding.mission.canonical_hash,
            "task_sha256": mission_binding.task.canonical_hash,
            "vehicle_identity_sha256": mission_binding.vehicle_identity_sha256,
            "bound_at_s": mission_binding.bound_at_s,
            "clock_epoch": mission_binding.clock_epoch,
        },
        "evidence_original": _encoded(payload),
        "boundary": dict(_BOUNDARY),
        "source_authentication": "host_registry_required_not_proven_by_hash",
        "clock_semantics": "explicit_host_epoch_not_sensor_clock_claim",
    }
    # Verify exact begin bytes and imported sources again immediately before final publication.
    record.to_mapping()
    if mission_binding.journal_path.read_bytes() != original:
        raise OriginalHostAuthoringError("original journal changed before publication")
    published_at_s = _clock(clock, clock_epoch, scope)
    if published_at_s < now_s:
        raise OriginalHostAuthoringError("actual seal clock regressed during validation")
    document["sealed_at_s"] = published_at_s
    sealed_payload = canonical_json(document).encode()
    path = _publish(output_dir, seal_name, sealed_payload)
    return SealedOriginalHostAuthoring(path, sealed_payload, _sha(sealed_payload))


__all__ = [
    "BEGIN_CONTRACT",
    "SEAL_CONTRACT",
    "OriginalHostAuthoringError",
    "OriginalHostAuthoringRecord",
    "SealedOriginalHostAuthoring",
    "begin_original_host_authoring",
    "seal_original_host_authoring",
]
