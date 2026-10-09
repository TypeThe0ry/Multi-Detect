"""Hash-bound identity for the shared autonomous task policy.

The policy artifact is deliberately metadata-only.  It does not contain a
flight-control command, a transport handle, simulator truth, or an execution
permission.  Its job is to make it impossible to silently use one candidate in
SIM and a different candidate in an observed HIL/REAL task session.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .serialization import (
    AutonomyContractError,
    canonical_sha256,
    require_schema_version,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)
from .task_ranker import FEATURES, TaskRanker

POLICY_ARTIFACT_SCHEMA_VERSION = 1
POLICY_ARTIFACT_CONTRACT = "multidetect.shared-task-policy-artifact"
MAX_POLICY_ARTIFACT_BYTES = 65536


@dataclass(frozen=True, slots=True)
class PolicyArtifact:
    """Identity and provenance of one candidate used by every runtime mode."""

    policy_id: str
    policy_version: str
    ranker_sha256: str
    airframe_profile_hash: str
    mission_contract_hash: str
    safety_policy_hash: str
    parent_model_sha256: str | None = None
    training_dataset_sha256: str | None = None
    schema_version: int = POLICY_ARTIFACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "schema_version", require_schema_version(self.schema_version)
        )
        for name in ("policy_id", "policy_version"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        for name in (
            "ranker_sha256",
            "airframe_profile_hash",
            "mission_contract_hash",
            "safety_policy_hash",
        ):
            object.__setattr__(self, name, require_sha256(getattr(self, name), name))
        for name in ("parent_model_sha256", "training_dataset_sha256"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, require_sha256(value, name))

    @classmethod
    def for_ranker(
        cls,
        ranker: TaskRanker,
        *,
        policy_id: str,
        policy_version: str,
        airframe_profile_hash: str,
        mission_contract_hash: str,
        safety_policy_hash: str,
        parent_model_sha256: str | None = None,
        training_dataset_sha256: str | None = None,
    ) -> PolicyArtifact:
        if not isinstance(ranker, TaskRanker):
            raise TypeError("ranker must be a TaskRanker")
        return cls(
            policy_id=policy_id,
            policy_version=policy_version,
            ranker_sha256=ranker.canonical_hash,
            airframe_profile_hash=airframe_profile_hash,
            mission_contract_hash=mission_contract_hash,
            safety_policy_hash=safety_policy_hash,
            parent_model_sha256=parent_model_sha256,
            training_dataset_sha256=training_dataset_sha256,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "contract": POLICY_ARTIFACT_CONTRACT,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "ranker_sha256": self.ranker_sha256,
            "feature_names": list(FEATURES),
            "airframe_profile_hash": self.airframe_profile_hash,
            "mission_contract_hash": self.mission_contract_hash,
            "safety_policy_hash": self.safety_policy_hash,
            "parent_model_sha256": self.parent_model_sha256,
            "training_dataset_sha256": self.training_dataset_sha256,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @classmethod
    def from_mapping(cls, value: object) -> PolicyArtifact:
        data = strict_object(
            value,
            name="policy artifact",
            required=frozenset(
                {
                    "schema_version",
                    "contract",
                    "policy_id",
                    "policy_version",
                    "ranker_sha256",
                    "feature_names",
                    "airframe_profile_hash",
                    "mission_contract_hash",
                    "safety_policy_hash",
                    "parent_model_sha256",
                    "training_dataset_sha256",
                }
            ),
        )
        require_schema_version(data["schema_version"])
        if data["contract"] != POLICY_ARTIFACT_CONTRACT:
            raise AutonomyContractError("unsupported policy artifact contract")
        if not isinstance(data["feature_names"], list):
            raise AutonomyContractError("policy artifact feature_names must be an array")
        if tuple(data["feature_names"]) != FEATURES:
            raise AutonomyContractError("policy artifact feature order does not match ranker")
        return cls(
            policy_id=data["policy_id"],
            policy_version=data["policy_version"],
            ranker_sha256=data["ranker_sha256"],
            airframe_profile_hash=data["airframe_profile_hash"],
            mission_contract_hash=data["mission_contract_hash"],
            safety_policy_hash=data["safety_policy_hash"],
            parent_model_sha256=data["parent_model_sha256"],
            training_dataset_sha256=data["training_dataset_sha256"],
            schema_version=data["schema_version"],
        )


def load_policy_artifact(
    path: Path,
    *,
    expected_sha256: str,
    ranker: TaskRanker,
) -> PolicyArtifact:
    """Load a strict JSON artifact and prove it names the supplied ranker."""

    expected = require_sha256(expected_sha256, "expected_sha256")
    import hashlib

    with path.open("rb") as stream:
        raw = stream.read(MAX_POLICY_ARTIFACT_BYTES + 1)
    if len(raw) > MAX_POLICY_ARTIFACT_BYTES:
        raise AutonomyContractError("policy artifact exceeds size limit")

    if hashlib.sha256(raw).hexdigest() != expected:
        raise AutonomyContractError("policy artifact hash mismatch")
    artifact = PolicyArtifact.from_mapping(strict_json_loads(raw, name="policy artifact"))
    if not isinstance(ranker, TaskRanker):
        raise TypeError("ranker must be a TaskRanker")
    if artifact.ranker_sha256 != ranker.canonical_hash:
        raise AutonomyContractError("policy artifact ranker hash does not match candidate")
    return artifact


__all__ = [
    "POLICY_ARTIFACT_CONTRACT",
    "POLICY_ARTIFACT_SCHEMA_VERSION",
    "MAX_POLICY_ARTIFACT_BYTES",
    "PolicyArtifact",
    "load_policy_artifact",
]
