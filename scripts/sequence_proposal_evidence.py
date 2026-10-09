"""Bounded source binding for offline sequence proposal reports.

These checks establish file identity and known-input/declared-family
separation only. They cannot prove semantic or physical independence.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from multidetect.autonomy.policy_artifact import PolicyArtifact
from multidetect.autonomy.policy_contract import SharedTaskPolicy
from multidetect.autonomy.serialization import canonical_sha256, require_sha256, strict_json_loads
from multidetect.autonomy.task_ranker import TaskRanker
from scripts.train_task_ranker import load_dataset

ROOT = Path(__file__).resolve().parents[1]
DATASET_MAX_BYTES = 4 * 1024 * 1024
POLICY_MAX_BYTES = 65536
ALGORITHM_SOURCES = (
    "src/multidetect/autonomy/task_sequence_reference.py",
    "src/multidetect/autonomy/policy_contract.py",
    "src/multidetect/autonomy/policy_artifact.py",
    "src/multidetect/autonomy/task_ranker.py",
    "src/multidetect/autonomy/task_selection.py",
    "src/multidetect/autonomy/task_policy.py",
    "src/multidetect/autonomy/planner.py",
    "src/multidetect/autonomy/contracts.py",
    "src/multidetect/autonomy/serialization.py",
    "src/multidetect/simulation/task_episode.py",
    "src/multidetect/mission_planning.py",
    "src/multidetect/clock.py",
    "scripts/train_task_ranker.py",
    "scripts/sequence_proposal_evidence.py",
)


@dataclass(frozen=True)
class BoundSource:
    path: Path
    sha256: str
    byte_count: int
    maximum_bytes: int

    def revalidate(self) -> None:
        current = bind_source(self.path, self.sha256, self.maximum_bytes)
        if current.byte_count != self.byte_count:
            raise ValueError(f"source byte count changed: {self.path}")

    def to_mapping(self) -> dict[str, object]:
        return {"path": str(self.path), "file_sha256": self.sha256, "bytes": self.byte_count}


def read_bound_source(
    path: Path, expected_sha256: str, maximum_bytes: int
) -> tuple[BoundSource, bytes]:
    expected = require_sha256(expected_sha256, "expected_sha256")
    with path.open("rb") as stream:
        raw = stream.read(maximum_bytes + 1)
    if len(raw) > maximum_bytes:
        raise ValueError(f"source exceeds size limit: {path}")
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError(f"source hash mismatch or changed during evaluation: {path}")
    return BoundSource(path, expected, len(raw), maximum_bytes), raw


def bind_source(path: Path, expected_sha256: str, maximum_bytes: int) -> BoundSource:
    source, _ = read_bound_source(path, expected_sha256, maximum_bytes)
    return source


def load_bound_dataset(path: Path, expected_sha256: str):
    source, raw = read_bound_source(path, expected_sha256, DATASET_MAX_BYTES)
    groups, _, identity = load_dataset(path, raw=raw)
    if identity["sha256"] != source.sha256 or identity["bytes"] != source.byte_count:
        raise ValueError("parsed dataset identity changed")
    source.revalidate()
    return groups, identity, source


def load_bound_policy(
    ranker_path: Path,
    ranker_sha256: str,
    artifact_path: Path,
    artifact_sha256: str,
):
    ranker_source, ranker_raw = read_bound_source(
        ranker_path, ranker_sha256, POLICY_MAX_BYTES
    )
    artifact_source, artifact_raw = read_bound_source(
        artifact_path, artifact_sha256, POLICY_MAX_BYTES
    )
    ranker = TaskRanker.from_mapping(
        strict_json_loads(ranker_raw, name="task ranker")
    )
    artifact = PolicyArtifact.from_mapping(
        strict_json_loads(artifact_raw, name="policy artifact")
    )
    if artifact.ranker_sha256 != ranker.canonical_hash:
        raise ValueError("policy artifact does not bind the supplied ranker")
    return SharedTaskPolicy(ranker, artifact), ranker_source, artifact_source


def validate_known_holdout_separation(
    training_groups, training_identity, evaluation_groups, evaluation_identity, policy
) -> dict[str, object]:
    if policy.artifact.training_dataset_sha256 != training_identity["sha256"]:
        raise ValueError("training dataset does not bind policy artifact")
    # Include every training/validation/test input ever available for model
    # development; checking only the training split would allow validation reuse.
    training_cases = tuple(case for cases in training_groups.values() for case in cases)
    evaluation_cases = tuple(case for cases in evaluation_groups.values() for case in cases)
    training_hashes = {case.input_hash for case in training_cases}
    evaluation_hashes = {case.input_hash for case in evaluation_cases}
    if training_hashes & evaluation_hashes:
        raise ValueError("training and evaluation input hashes overlap")
    training_families = set(training_identity["families"])
    evaluation_families = set(evaluation_identity["families"])
    if training_families & evaluation_families:
        raise ValueError("training and evaluation scenario families overlap")
    expected_airframe = policy.artifact.airframe_profile_hash
    if any(case.airframe_profile_hash != expected_airframe for case in (
        *training_cases, *evaluation_cases
    )):
        raise ValueError("training/evaluation airframe does not bind policy artifact")
    all_cases = (*training_cases, *evaluation_cases)
    if any(
        case.context.airframe_profile_hash is None
        or case.context.airframe_profile_hash != case.airframe_profile_hash
        for case in all_cases
    ):
        raise ValueError("context airframe profile does not bind dataset case")
    return {
        "training_input_count": len(training_hashes),
        "evaluation_input_count": len(evaluation_hashes),
        "input_hashes_disjoint": True,
        "scenario_families_disjoint": True,
        "training_families": sorted(training_families),
        "evaluation_families": sorted(evaluation_families),
        "context_airframe_profile_bound": True,
        "context_airframe_profile_hashes": sorted(
            {case.context.airframe_profile_hash for case in all_cases}
        ),
        "scope": "known_inputs_and_declared_families_only",
        "semantic_independence_proven": False,
        "physical_independence_proven": False,
    }


def bind_algorithm_sources(entrypoint: Path):
    sources = []
    for path in (*[ROOT / name for name in ALGORITHM_SOURCES], entrypoint.resolve()):
        with path.open("rb") as stream:
            raw = stream.read(DATASET_MAX_BYTES + 1)
        if len(raw) > DATASET_MAX_BYTES:
            raise ValueError(f"algorithm source exceeds size limit: {path}")
        source = BoundSource(
            path,
            hashlib.sha256(raw).hexdigest(),
            len(raw),
            DATASET_MAX_BYTES,
        )
        sources.append(source)
    identities = [
        {**source.to_mapping(), "path": source.path.relative_to(ROOT).as_posix()}
        for source in sources
    ]
    return {
        "files": identities,
        "canonical_sha256": canonical_sha256({"files": identities}),
        "scope": "offline_evaluator_sources_not_deployment_parity",
    }, tuple(sources)
