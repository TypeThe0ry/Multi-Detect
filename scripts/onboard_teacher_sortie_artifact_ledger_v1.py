"""Finite actual camera/native/evidence writes, not an issuer or flight Grant.

Reservations are permanently charged and fsynced before the caller's write.
Only committed exact original files count as source evidence. Unknown files,
partial writes, replacements and unrecorded native output refuse start capacity.
"""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass
from functools import wraps
from pathlib import Path

from multidetect.autonomy.onboard_executed_trajectory import EvidenceBinding
from multidetect.autonomy.serialization import (
    canonical_json,
    require_int,
    require_real,
    require_sha256,
    require_text,
)
from scripts.onboard_teacher_sortie_budget_v1 import TeacherSortieLifecycleBudget

CONTRACT = "multidetect.teacher-sortie-artifact-ledger-v1"


def _serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return call


def _identity(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino


@dataclass(frozen=True, slots=True)
class LedgerWriteReservation:
    index: int
    category: str
    path: str
    maximum_bytes: int
    budget_sha256: str


@dataclass(frozen=True, slots=True)
class LedgerFileBinding:
    """Retain honest zero-byte native stderr; never claim nonempty evidence."""

    path: str
    bytes: int
    sha256: str

    def __post_init__(self):
        require_text(self.path, "original file path")
        require_int(self.bytes, "original file bytes", minimum=0)
        require_sha256(self.sha256, "original file SHA")

    def to_mapping(self):
        return {"path": self.path, "bytes": self.bytes, "sha256": self.sha256}


class TeacherSortieArtifactLedger:
    @property
    def camera_directory(self):
        return self._roots["camera"]

    @property
    def evidence_directory(self):
        return self._roots["evidence"]

    def __init__(self, *, budget, clock, camera_directory, evidence_directory):
        if type(budget) is not TeacherSortieLifecycleBudget or not callable(clock):
            raise TypeError("Exact once-bound budget/actual clock required")
        self.budget, self._clock = budget, clock
        self._budget_sha256 = budget.canonical_hash
        self._owner = threading.get_ident()
        self._lock = threading.RLock()
        self._sender_receiver = None
        self._last_now = budget.started_at_s
        self._closed = False
        self._failure = None
        self._charged = {"camera": 0, "evidence": 0}
        self._pending, self._committed = {}, {}
        self._next_index = 0
        self._roots = {
            "camera": Path(camera_directory).absolute(),
            "evidence": Path(evidence_directory).absolute(),
        }
        roots = list(self._roots.values())
        if roots[0].is_relative_to(roots[1]) or roots[1].is_relative_to(roots[0]):
            raise ValueError("Separate non-overlapping camera/evidence scopes required")
        for root in roots:
            if root.exists() or root.resolve() != root or any(p.is_symlink() for p in root.parents):
                raise ValueError("Fresh non-symlink artifact scope required")
        for root in roots:
            root.mkdir(parents=False, exist_ok=False)
        self._root_ids = {kind: _identity(root)[2:] for kind, root in self._roots.items()}
        self._journal_path = self._roots["evidence"] / "artifact-ledger.jsonl"
        self._stream = self._journal_path.open("xb+", buffering=0)
        self._journal_size = 0
        self._journal_digest = hashlib.sha256()
        self._journal_id = _identity(self._journal_path)[2:]
        self._save(
            {
                "contract": CONTRACT,
                "budget": budget.to_mapping(),
                "budget_sha256": self._budget_sha256,
            }
        )

    def _scope(self):
        owner = threading.get_ident()
        receiver = self._sender_receiver
        if owner != self._owner and not (
            receiver is not None
            and receiver._thread is not None
            and receiver._thread.ident == owner
            and receiver._thread.is_alive()
            and not receiver._stop.is_set()
        ):
            raise RuntimeError("Artifact ledger has only its main and exact execution RX owner")
        if self._closed or self._failure:
            raise RuntimeError("Artifact ledger is closed or poisoned")
        if self.budget.canonical_hash != self._budget_sha256:
            raise ValueError("Original artifact budget changed")
        now = require_real(self._clock(), "ledger actual clock", minimum=self.budget.started_at_s)
        if now < self._last_now:
            raise ValueError("Actual artifact lifecycle clock regressed")
        self._last_now = now
        if self.budget.remaining_s(now, clock_id=self.budget.clock_id) <= 0:
            raise ValueError("Original artifact lifecycle exhausted")
        for kind, root in self._roots.items():
            if (
                root.is_symlink()
                or root.resolve() != root
                or _identity(root)[2:] != self._root_ids[kind]
            ):
                raise ValueError("Original artifact root identity changed")
        return now

    @_serialized
    def _save(self, row):
        self._scope()
        raw = (canonical_json(row) + "\n").encode()
        if len(raw) > 65536 or self._charged["evidence"] + len(raw) > self.budget.evidence_bytes:
            raise ValueError("Finite artifact-ledger evidence allocation exhausted")
        before = _identity(self._journal_path)
        if (
            self._journal_path.is_symlink()
            or before[0] != self._journal_size
            or before[2:] != self._journal_id
        ):
            raise ValueError("Ledger journal replaced or appended outside its owner")
        self._charged["evidence"] += len(raw)
        try:
            self._stream.seek(self._journal_size)
            if self._stream.write(raw) != len(raw):
                raise OSError("Short ledger journal write")
            self._stream.flush()
            os.fsync(self._stream.fileno())
            self._stream.seek(self._journal_size)
            if (
                self._stream.read(len(raw) + 1) != raw
                or _identity(self._journal_path)[2:] != self._journal_id
            ):
                raise ValueError("Ledger intention/result differs after fsync")
            self._scope()
            self._journal_size += len(raw)
            self._journal_digest.update(raw)
        except BaseException as error:
            self._failure = str(error)
            raise

    @_serialized
    def bind_execution_sender(self, receiver):
        from scripts.onboard_teacher_mission_transport_v1 import _ConcurrentReceiverV6

        self._scope()
        if (
            threading.get_ident() != self._owner
            or self._sender_receiver is not None
            or type(receiver) is not _ConcurrentReceiverV6
            or receiver._owner != self._owner
            or receiver.clock != self._clock
            or receiver._thread is None
            or not receiver._thread.is_alive()
            or receiver._stop.is_set()
        ):
            raise ValueError("One actual existing live RX owner may bind the artifact ledger")
        self._sender_receiver = receiver
        self._save({"event": "bounded-execution-RX-bound", "thread_id": receiver._thread.ident})

    @_serialized
    def reserve_write(self, category, path, *, maximum_bytes):
        self._scope()
        if category not in self._roots or self._next_index >= 4096:
            raise ValueError("Bounded camera/native-evidence category/record required")
        target = Path(path).absolute()
        root = self._roots[category]
        size = require_int(maximum_bytes, "permanently charged write bytes", minimum=1)
        if (
            not target.is_relative_to(root)
            or target == root
            or target.exists()
            or target.resolve() != target
            or any(p.is_symlink() for p in target.parents)
            or not target.parent.is_dir()
            or target == self._journal_path
        ):
            raise ValueError("Fresh contained source path required before native/write API")
        if any(r.path == target.as_posix() for r in self._pending.values()):
            raise ValueError("Source path already reserved")
        total = self.budget.camera_bytes if category == "camera" else self.budget.evidence_bytes
        # Leave a bounded intention/result allowance; journal bytes are charged
        # separately as written. No refund if an external/native API fails.
        margin = 8192 if category == "evidence" else 0
        if self._charged[category] + size + margin > total:
            raise ValueError("Declared camera/native-evidence capacity exhausted")
        reservation = LedgerWriteReservation(
            self._next_index, category, target.as_posix(), size, self._budget_sha256
        )
        self._charged[category] += size
        self._next_index += 1
        self._pending[reservation.index] = reservation
        self._save(
            {
                "event": "reserved-before-write",
                "reservation": {
                    "index": reservation.index,
                    "category": category,
                    "path": reservation.path,
                    "maximum_bytes": size,
                    "budget_sha256": self._budget_sha256,
                },
            }
        )
        return reservation

    @_serialized
    def commit_write(self, reservation, binding):
        self._scope()
        if (
            type(reservation) is not LedgerWriteReservation
            or self._pending.get(reservation.index) is not reservation
            or type(binding) not in (EvidenceBinding, LedgerFileBinding)
        ):
            raise TypeError("Exact ledger-owned reservation and original source binding required")
        path = Path(reservation.path)
        if binding.path != path.as_posix() or not 0 <= binding.bytes <= reservation.maximum_bytes:
            raise ValueError("Native source differs from its pre-write reservation")
        before = _identity(path)
        if path.is_symlink() or before[0] != binding.bytes:
            raise ValueError("Actual reserved file identity/size differs")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while raw := stream.read(1024 * 1024):
                digest.update(raw)
        if _identity(path) != before or digest.hexdigest() != binding.sha256:
            raise ValueError("Actual reserved file bytes/SHA changed")
        self._save(
            {
                "event": "committed-original-source",
                "index": reservation.index,
                "source": binding.to_mapping(),
                "reserved_bytes_not_refunded": True,
            }
        )
        self._committed[path.as_posix()] = (binding, before)
        del self._pending[reservation.index]
        return binding

    @_serialized
    def require_source(self, binding, *, artifact_root, category=None):
        """The source must really be retained by this pre-write ledger."""
        self._scope()
        if type(binding) is not EvidenceBinding:
            raise TypeError("Typed original dynamic source binding required")
        path = (Path(artifact_root).resolve() / binding.path).absolute()
        known = self._committed.get(path.as_posix())
        if known is None or known[0].bytes != binding.bytes or known[0].sha256 != binding.sha256:
            raise ValueError(
                "Current dynamic source was not committed through the lifecycle ledger"
            )
        if category is not None and not path.is_relative_to(self._roots[category]):
            raise ValueError("Current source belongs to a different resource scope")
        if path.is_symlink() or _identity(path) != known[1]:
            raise ValueError("Current retained dynamic source identity changed")
        return binding

    @_serialized
    def remaining_for_bound_sender(self):
        """Actual charged counters only; not a file audit or admission proof."""
        self._scope()
        if self._pending:
            raise ValueError("Uncommitted native/write reservation remains before control")
        return {
            "remaining_camera_bytes": self.budget.camera_bytes - self._charged["camera"],
            "remaining_evidence_bytes": self.budget.evidence_bytes - self._charged["evidence"],
        }

    @_serialized
    def capacity(self):
        self._scope()
        if threading.get_ident() != self._owner:
            raise RuntimeError("Only main may inspect current start capacity")
        if self._pending:
            raise ValueError("Pending/partial/unobserved artifact write: no start capacity")
        expected = {*self._committed, self._journal_path.as_posix()}
        found = set()
        for root in self._roots.values():
            for path in root.rglob("*"):
                if path.is_symlink():
                    raise ValueError("Symlink appeared inside original artifact scope")
                if path.is_file():
                    found.add(path.as_posix())
                    if len(found) > 4097:
                        raise ValueError("Finite artifact file count exceeded")
        if found != expected:
            raise ValueError("Unknown/missing native source in actual artifact scopes")
        for relative, (binding, before) in self._committed.items():
            path = Path(relative)
            if _identity(path) != before:
                raise ValueError("Known committed source identity changed")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while raw := stream.read(1024 * 1024):
                    digest.update(raw)
            if digest.hexdigest() != binding.sha256 or _identity(path) != before:
                raise ValueError("Known committed source bytes changed")
        before = _identity(self._journal_path)
        if before[0] != self._journal_size or before[2:] != self._journal_id:
            raise ValueError("Ledger physical generation changed")
        digest = hashlib.sha256()
        with self._journal_path.open("rb") as stream:
            while raw := stream.read(1024 * 1024):
                digest.update(raw)
        if (
            digest.hexdigest() != self._journal_digest.hexdigest()
            or _identity(self._journal_path) != before
        ):
            raise ValueError("Ledger original journal SHA changed")
        self._scope()
        return {
            "remaining_camera_bytes": self.budget.camera_bytes - self._charged["camera"],
            "remaining_evidence_bytes": self.budget.evidence_bytes - self._charged["evidence"],
        }

    @_serialized
    def close(self):
        if threading.get_ident() != self._owner or self._closed:
            raise RuntimeError("Artifact ledger can close only once on its main owner")
        self._closed = True
        self._stream.close()


__all__ = ["TeacherSortieArtifactLedger", "LedgerWriteReservation", "LedgerFileBinding"]
