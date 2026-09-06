"""Evaluation-side loader for all three isolated scenario channels."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..autonomy.serialization import AutonomyContractError
from ..runtime import RuntimeMode
from .index import (
    ScenarioManifestIndex,
    load_scenario_index,
    load_verified_channel,
)
from .runtime_input import ScenarioRuntimeInput
from .truth import ScenarioExpectations, ScenarioTruth


@dataclass(frozen=True, slots=True)
class ScenarioManifest:
    """Evaluator-only aggregate; never pass this object to RuntimeBackend."""

    index: ScenarioManifestIndex
    runtime_input: ScenarioRuntimeInput
    truth: ScenarioTruth
    expectations: ScenarioExpectations

    def __post_init__(self) -> None:
        if not isinstance(self.index, ScenarioManifestIndex):
            raise AutonomyContractError("index must be ScenarioManifestIndex")
        if not isinstance(self.runtime_input, ScenarioRuntimeInput):
            raise AutonomyContractError("runtime_input must be ScenarioRuntimeInput")
        if not isinstance(self.truth, ScenarioTruth):
            raise AutonomyContractError("truth must be ScenarioTruth")
        if not isinstance(self.expectations, ScenarioExpectations):
            raise AutonomyContractError("expectations must be ScenarioExpectations")
        identity = (self.index.scenario_id, self.index.scenario_version)
        for name, channel in (
            ("runtime_input", self.runtime_input),
            ("truth", self.truth),
            ("expectations", self.expectations),
        ):
            if (channel.scenario_id, channel.scenario_version) != identity:
                raise AutonomyContractError(f"{name} scenario identity does not match manifest")
        if self.runtime_input.scenario_manifest_hash != self.index.canonical_hash:
            raise AutonomyContractError(
                "runtime_input scenario_manifest_hash does not match manifest"
            )

    @property
    def scenario_id(self) -> str:
        return self.index.scenario_id

    @property
    def scenario_version(self) -> int:
        return self.index.scenario_version

    @property
    def schema_version(self) -> int:
        return self.index.schema_version

    def runtime_input_for(self, mode: RuntimeMode) -> ScenarioRuntimeInput:
        if not isinstance(mode, RuntimeMode):
            raise AutonomyContractError("mode must be a RuntimeMode")
        if mode is RuntimeMode.REAL:
            raise AutonomyContractError("REAL runtime cannot load a scenario or injected faults")
        return self.runtime_input

    def to_mapping(self) -> dict[str, Any]:
        return self.index.to_mapping()

    @property
    def canonical_json(self) -> str:
        return self.index.canonical_json

    @property
    def canonical_hash(self) -> str:
        return self.index.canonical_hash

    @property
    def canonical_sha256(self) -> str:
        return self.canonical_hash


def load_scenario_manifest(path: str | Path) -> ScenarioManifest:
    source, index = load_scenario_index(path)
    runtime_raw = load_verified_channel(source, index.runtime_input, name="runtime_input")
    truth_raw = load_verified_channel(source, index.truth, name="truth")
    expectations_raw = load_verified_channel(source, index.expectations, name="expectations")
    return ScenarioManifest(
        index=index,
        runtime_input=ScenarioRuntimeInput.from_mapping(
            runtime_raw,
            scenario_manifest_hash=index.canonical_hash,
        ),
        truth=ScenarioTruth.from_mapping(truth_raw),
        expectations=ScenarioExpectations.from_mapping(expectations_raw),
    )


__all__ = ["ScenarioManifest", "load_scenario_manifest"]
